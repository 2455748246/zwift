#!/usr/bin/env python

import calendar
import datetime
import logging
import os
import platform
import random
import sqlite3
import sys
import tempfile
import time
import math
from copy import copy
from datetime import timedelta
from functools import wraps
from io import BytesIO
from shutil import copyfile

import jwt
from flask import Flask, request, jsonify, g, redirect, render_template, url_for, flash, session, abort, make_response, send_file
from flask_login import UserMixin, AnonymousUserMixin, LoginManager, login_user, current_user, login_required
from google.protobuf.descriptor import FieldDescriptor
from protobuf_to_dict import protobuf_to_dict, TYPE_CALLABLE_MAP
from flask_sqlalchemy import sqlalchemy, SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

import protobuf.udp_node_msgs_pb2 as udp_node_msgs_pb2
import protobuf.activity_pb2 as activity_pb2
import protobuf.goal_pb2 as goal_pb2
import protobuf.login_response_pb2 as login_response_pb2
import protobuf.per_session_info_pb2 as per_session_info_pb2
import protobuf.periodic_info_pb2 as periodic_info_pb2
import protobuf.profile_pb2 as profile_pb2
import protobuf.segment_result_pb2 as segment_result_pb2
import protobuf.world_pb2 as world_pb2
import protobuf.zfiles_pb2 as zfiles_pb2
import protobuf.hash_seeds_pb2 as hash_seeds_pb2

logging.basicConfig(filename='zoffline.log', level=os.environ.get("LOGLEVEL", "INFO"))
logger = logging.getLogger('zoffline')
logger.setLevel(logging.WARN)

if os.name == 'nt' and platform.release() == '10' and platform.version() >= '10.0.14393':
    # Fix ANSI color in Windows 10 version 10.0.14393 (Windows Anniversary Update)
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)

if getattr(sys, 'frozen', False):
    # If we're running as a pyinstaller bundle
    SCRIPT_DIR = sys._MEIPASS
    STORAGE_DIR = "%s/storage" % os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
    STORAGE_DIR = "%s/storage" % SCRIPT_DIR

try:
    # Ensure storage dir exists
    if not os.path.isdir(STORAGE_DIR):
        os.makedirs(STORAGE_DIR)
except IOError as e:
    logger.error("failed to create storage dir (%s):  %s", STORAGE_DIR, str(e))
    sys.exit(1)

SSL_DIR = "%s/ssl" % SCRIPT_DIR
DATABASE_INIT_SQL = "%s/initialize_db.sql" % SCRIPT_DIR
DATABASE_PATH = "%s/zwift-offline.db" % STORAGE_DIR
DATABASE_CUR_VER = 2

# For auth server
AUTOLAUNCH_FILE = "%s/auto_launch.txt" % STORAGE_DIR
SERVER_IP_FILE = "%s/server-ip.txt" % STORAGE_DIR
SECRET_KEY_FILE = "%s/secret-key.txt" % STORAGE_DIR
MULTIPLAYER = False
if os.path.exists("%s/multiplayer.txt" % STORAGE_DIR):
    MULTIPLAYER = True
from tokens import *

# Android uses https for cdn
app = Flask(__name__, static_folder='%s/cdn/gameassets' % SCRIPT_DIR, static_url_path='/gameassets', template_folder='%s/cdn/static/web/launcher' % SCRIPT_DIR)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///{db}'.format(db=DATABASE_PATH)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
if not os.path.exists(SECRET_KEY_FILE):
    with open(SECRET_KEY_FILE, 'wb') as f:
        f.write(os.urandom(16))
with open(SECRET_KEY_FILE, 'rb') as f:
    app.config['SECRET_KEY'] = f.read()
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024

db = SQLAlchemy(app)
online = {}
ghostsEnabled = {}
playerUpdateQueue = {}
playerIds = {}
playerPartialProfiles = {}
saveGhost = None

class User(UserMixin, db.Model):
    player_id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    pass_hash = db.Column(db.String(100), nullable=False)
    enable_ghosts = db.Column(db.Integer, nullable=False, default=1)

    def __repr__(self):
        return self.username

    def get_id(self):
        return self.player_id

class AnonUser(User, AnonymousUserMixin, db.Model):
    username = "zoffline"
    first_name = "z"
    last_name = "offline"
    enable_ghosts = True

    def is_authenticated(self):
        return True

class PartialProfile:
    first_name = ''
    last_name = ''
    country_code = 0

class Online:
    total = 0
    richmond = 0
    watopia = 0
    london = 0
    newyork = 0
    innsbruck = 0
    yorkshire = 0
    france = 0
    paris = 0

coursesLookup = {
    2: 'Richmond',
    4: 'Unknown1',  # event specific?
    6: 'Watopia',
    7: 'London',
    8: 'New York',
    9: 'Innsbruck',
    10: 'Unknown2',  # event specific?
    11: 'Yorkshire',
    12: 'Unknown3',  # event specific?
    14: 'France',
    15: 'Paris'
}

def getUTCDateTime():
    return datetime.datetime.utcnow()

def getUTCTime():
    return getUTCDateTime().timestamp()

def getOnline():
    onlineInRegion = Online()
    for p_id in online:
        player_state = online[p_id]
        course = getCourse(player_state)
        course_name = coursesLookup[course]
        if course_name == 'Richmond':
            onlineInRegion.richmond += 1
        elif course_name == 'Watopia':
            onlineInRegion.watopia += 1
        elif course_name == 'London':
            onlineInRegion.london += 1
        elif course_name == 'New York':
            onlineInRegion.newyork += 1
        elif course_name == 'Innsbruck':
            onlineInRegion.innsbruck += 1
        elif course_name == 'Yorkshire':
            onlineInRegion.yorkshire += 1
        elif course_name == 'France':
            onlineInRegion.france += 1
        elif course_name == 'Paris':
            onlineInRegion.paris += 1
        onlineInRegion.total += 1
    return onlineInRegion


def getPartialProfile(player_id):
    if not player_id in playerPartialProfiles:
        #Read from disk
        profile_file = '%s/%s/profile.bin' % (STORAGE_DIR, player_id)
        if os.path.isfile(profile_file):
            try:
                with open(profile_file, 'rb') as fd:
                    profile = profile_pb2.Profile()
                    profile.ParseFromString(fd.read())
                    partialProfile = PartialProfile()
                    partialProfile.first_name = profile.first_name
                    partialProfile.last_name = profile.last_name
                    partialProfile.country_code = profile.country_code
                    playerPartialProfiles[player_id] = partialProfile
            except:
                return None
        else: return None
    return playerPartialProfiles[player_id]

def getCourse(state):
    return (state.f19 & 0xff0000) >> 16

def isNearby(player_state1, player_state2, range = 100000):
    try:
        course1 = getCourse(player_state1)
        course2 = getCourse(player_state2)
        if course1 == course2:
            x1 = int(player_state1.x)
            x2 = int(player_state2.x)
            if x1 - range <= x2 and x1 + range >= x2:
                y1 = int(player_state1.y)
                y2 = int(player_state2.y)
                if y1 - range <= y2 and y1 + range >= y2:
                    a1 = int(player_state1.altitude)
                    a2 = int(player_state2.altitude)
                    if a1 - range <= a2 and a1 + range >= a2:
                        return True
    except:
        pass
    return False


# We store flask-login's cookie in the "fake" JWT that we give Zwift.
# Make it a cookie again to reuse flask-login on API calls.
def jwt_to_session_cookie(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not MULTIPLAYER:
            return f(*args, **kwargs)
        token = request.headers.get('Authorization')
        if token and not session.get('_user_id'):
            token = jwt.decode(token.split()[1], options=({'verify_signature': False, 'verify_aud': False}))
            request.cookies = request.cookies.copy()  # request.cookies is an immutable dict
            request.cookies['remember_token'] = token['session_cookie']
            login_manager._load_user()

        return f(*args, **kwargs)
    return wrapper


@app.route("/signup/", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form['username']
        password = request.form['password']
        confirm_password = request.form['confirm_password']
        first_name = request.form['first_name']
        last_name = request.form['last_name']

        if not (username and password and confirm_password and first_name and last_name):
            flash("All fields are required.")
            return redirect(url_for('signup'))
        if password != confirm_password:
            flash("Passwords did not match.")
            return redirect(url_for('signup'))

        hashed_pwd = generate_password_hash(password, 'sha256')

        new_user = User(username=username, pass_hash=hashed_pwd, first_name=first_name, last_name=last_name)
        db.session.add(new_user)

        try:
            db.session.commit()
        except sqlalchemy.exc.IntegrityError:
            flash("Username {u} is not available.".format(u=username))
            return redirect(url_for('signup'))

        flash("User account has been created.")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/login/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form['username']
        password = request.form['password']

        if not (username and password):
            flash("Username and password cannot be empty.")
            return redirect(url_for('login'))

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.pass_hash, password):
            login_user(user, remember=True)
            return redirect(url_for("user_home", username=username, enable_ghosts=bool(user.enable_ghosts), online=getOnline()))
        else:
            flash("Invalid username or password.")

    return render_template("login_form.html")


@app.route("/user/<username>/")
@login_required
def user_home(username):
    return render_template("user_home.html", username=current_user.username, enable_ghosts=bool(current_user.enable_ghosts), online=getOnline())


@app.route("/upload/<username>/", methods=["GET", "POST"])
@login_required
def upload(username):
    player_id = current_user.player_id
    profile_dir = os.path.join(STORAGE_DIR, str(player_id))
    try:
        if not os.path.isdir(profile_dir):
            os.makedirs(profile_dir)
    except IOError as e:
        logger.error("failed to create profile dir (%s):  %s", profile_dir, str(e))
        return '', 500

    if request.method == 'POST':
        uploaded_file = request.files['file']
        if uploaded_file.filename in ['profile.bin', 'strava_token.txt']:
            uploaded_file.save(os.path.join(profile_dir, uploaded_file.filename))
            flash("File %s uploaded." % uploaded_file.filename)
        else:
            flash("Invalid file name.")

    name = ''
    profile = None
    profile_file = os.path.join(profile_dir, 'profile.bin')
    if os.path.isfile(profile_file):
        stat = os.stat(profile_file)
        profile = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))
        with open(profile_file, 'rb') as fd:
            p = profile_pb2.Profile()
            p.ParseFromString(fd.read())
            name = "%s %s" % (p.first_name, p.last_name)
    token = None
    token_file = os.path.join(profile_dir, 'strava_token.txt')
    if os.path.isfile(token_file):
        stat = os.stat(token_file)
        token = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime))

    return render_template("upload.html", username=current_user.username, profile=profile, name=name, token=token)


@app.route("/download/profile.bin", methods=["GET"])
@login_required
def download():
    player_id = current_user.player_id
    profile_dir = os.path.join(STORAGE_DIR, str(player_id))
    profile_file = os.path.join(profile_dir, 'profile.bin')
    if os.path.isfile(profile_file):
        return send_file(profile_file, attachment_filename='profile.bin')


@app.route("/logout/<username>")
def logout(username):
    flash("Successfully logged out.")
    return redirect(url_for('login'))


####
# Set up protobuf_to_dict call map
type_callable_map = copy(TYPE_CALLABLE_MAP)
# Override base64 encoding of byte fields
type_callable_map[FieldDescriptor.TYPE_BYTES] = str
# sqlite doesn't support uint64 so make them strings
type_callable_map[FieldDescriptor.TYPE_UINT64] = str


def insert_protobuf_into_db(table_name, msg):
    msg_dict = protobuf_to_dict(msg, type_callable_map=type_callable_map)
    columns = ', '.join(list(msg_dict.keys()))
    placeholders = ':'+', :'.join(list(msg_dict.keys()))
    query = 'INSERT INTO %s (%s) VALUES (%s)' % (table_name, columns, placeholders)
    db.engine.execute(query, msg_dict)


# XXX: can't be used to 'nullify' a column value
def update_protobuf_in_db(table_name, msg, id):
    try:
        # If protobuf has an id field and it's uint64, make it a string
        id_field = msg.DESCRIPTOR.fields_by_name['id']
        if id_field.type == id_field.TYPE_UINT64:
            id = str(id)
    except AttributeError:
        pass
    msg_dict = protobuf_to_dict(msg, type_callable_map=type_callable_map)
    columns = ', '.join(list(msg_dict.keys()))
    placeholders = ':'+', :'.join(list(msg_dict.keys()))
    setters = ', '.join('{}=:{}'.format(key, key) for key in msg_dict)
    query = 'UPDATE %s SET %s WHERE id=%s' % (table_name, setters, id)
    db.engine.execute(query, msg_dict)


def row_to_protobuf(row, msg, exclude_fields=[]):
    for key in list(msg.DESCRIPTOR.fields_by_name.keys()):
        if key in exclude_fields:
            continue
        if row[key] is None:
            continue
        field = msg.DESCRIPTOR.fields_by_name[key]
        if field.type == field.TYPE_UINT64:
            setattr(msg, key, int(row[key]))
        else:
            setattr(msg, key, row[key])
    return msg


# FIXME: I should really do this properly...
def get_id(table_name):
    while True:
        # I think activity id is actually only uint32. On the off chance it's
        # int32, stick with 31 bits.
        ident = int(random.getrandbits(31))
        row = db.engine.execute("SELECT id FROM %s WHERE id = %s" % (table_name, ident)).first()
        if not row:
            break
    return ident


def world_time():
    return int((getUTCTime()-1414016075)*1000)


@app.route('/api/auth', methods=['GET'])
def api_auth():
    return '{"realm":"zwift","launcher":"https://launcher.zwift.com/launcher","url":"https://secure.zwift.com/auth/"}'


@app.route('/api/users/login', methods=['POST'])
def api_users_login():
    # Should just return a binary blob rather than build a "proper" response...
    response = login_response_pb2.LoginResponse()
    response.session_state = 'abc'
    response.info.relay_url = "https://us-or-rly101.zwift.com/relay"
    response.info.apis.todaysplan_url = "https://whats.todaysplan.com.au"
    response.info.apis.trainingpeaks_url = "https://api.trainingpeaks.com"
    response.info.time = int(getUTCTime())
    udp_node = response.info.nodes.node.add()
    if os.path.exists(SERVER_IP_FILE):
        with open(SERVER_IP_FILE, 'r') as f:
            udp_node.ip = f.read().rstrip('\r\n')
    else:
        udp_node.ip = "127.0.0.1"  # TCP telemetry server
    udp_node.port = 3023
    return response.SerializeToString(), 200


@app.route('/api/users/logout', methods=['POST'])
@jwt_to_session_cookie
@login_required
def api_users_logout():
    #Remove player from online when leaving game/world
    player_id = current_user.player_id
    if player_id in online:
        online.pop(player_id)
    if player_id in playerPartialProfiles:
        playerPartialProfiles.pop(player_id)
    return '', 204


@app.route('/api/analytics/event', methods=['POST'])
def api_analytics_event():
    return '', 200


@app.route('/api/per-session-info', methods=['GET'])
def api_per_session_info():
    info = per_session_info_pb2.PerSessionInfo()
    info.relay_url = "https://us-or-rly101.zwift.com/relay"
    return info.SerializeToString(), 200


@app.route('/api/events/search', methods=['POST'])
def api_events_search():
    return '', 200


@app.route('/api/zfiles', methods=['POST'])
def api_zfiles():
    # Don't care about zfiles, but shuts up some errors in Zwift log.
    zfile = zfiles_pb2.ZFile()
    zfile.id = int(random.getrandbits(31))
    zfile.folder = "logfiles"
    zfile.filename = "yep_took_good_care_of_that_file.txt"
    zfile.timestamp = int(getUTCTime())
    return zfile.SerializeToString(), 200


# Probably don't need, haven't investigated
@app.route('/api/zfiles/list', methods=['GET', 'POST'])
def api_zfiles_list():
    return '', 200


# Probably don't need, haven't investigated
@app.route('/api/private_event/feed', methods=['GET', 'POST'])
def api_private_event_feed():
    return '', 200


# Disable telemetry (shuts up some errors in log)
@app.route('/api/telemetry/config', methods=['GET'])
def api_telemetry_config():
    return '{"isEnabled":false}'


@app.route('/api/profiles/me', methods=['GET'])
@jwt_to_session_cookie
@login_required
def api_profiles_me():
    profile_id = current_user.player_id
    if MULTIPLAYER:
        profile_dir = '%s/%s' % (STORAGE_DIR, profile_id)
    else:
        # Find first profile.bin if one exists and use it. Multi-profile
        # support is deprecated and now unsupported for non-multiplayer mode.
        profile_dir = None
        for name in os.listdir(STORAGE_DIR):
            path = "%s/%s" % (STORAGE_DIR, name)
            if os.path.isdir(path) and os.path.exists("%s/profile.bin" % path):
                profile_dir = path
                break
        if not profile_dir:  # no existing profile
            profile_dir = "%s/1" % STORAGE_DIR
            profile_id = 1
            AnonUser.player_id = profile_id

    try:
        if not os.path.isdir(profile_dir):
            os.makedirs(profile_dir)
    except IOError as e:
        logger.error("failed to create profile dir (%s):  %s", profile_dir, str(e))
        return '', 500
    profile = profile_pb2.Profile()
    profile_file = '%s/profile.bin' % profile_dir
    if not os.path.isfile(profile_file):
        profile.id = profile_id
        profile.is_connected_to_strava = True
        profile.email = current_user.username
        profile.first_name = current_user.first_name
        profile.last_name = current_user.last_name
        return profile.SerializeToString(), 200
    with open(profile_file, 'rb') as fd:
        profile.ParseFromString(fd.read())
        if MULTIPLAYER:
            # For newly added existing profiles, User's player id likely differs from profile's player id.
            # If there's existing data in db for this profile, update it for the newly assigned player id.
            # XXX: Users can maliciously abuse this by intentionally uploading a profile with another user's current player id.
            #      However, without it, anyone "upgrading" to multiplayer mode will lose their existing data.
            # TODO: need a warning in README that switching to multiplayer mode and back to single player will lose your existing data.
            if profile.id != profile_id:
                db.engine.execute('UPDATE activity SET player_id = ? WHERE player_id = ?', (str(profile_id), str(profile.id)))
                db.engine.execute('UPDATE goal SET player_id = ? WHERE player_id = ?', (str(profile_id), str(profile.id)))
                db.engine.execute('UPDATE segment_result SET player_id = ? WHERE player_id = ?', (str(profile_id), str(profile.id)))
            profile.id = profile_id
        elif current_user.player_id != profile.id:
            # Update AnonUser's player_id to match
            AnonUser.player_id = profile.id
            ghostsEnabled[profile.id] = AnonUser.enable_ghosts
        if not profile.email:
            profile.email = 'user@email.com'
        if profile.f60:
            del profile.f60[:]
        return profile.SerializeToString(), 200


@app.route('/api/profiles/<int:player_id>', methods=['PUT'])
@jwt_to_session_cookie
@login_required
def api_profiles_id(player_id):
    if not request.stream:
        return '', 400
    if current_user.player_id != player_id:
        return '', 401
    stream = request.stream.read()
    with open('%s/%s/profile.bin' % (STORAGE_DIR, player_id), 'wb') as f:
        f.write(stream)
    profile = profile_pb2.Profile()
    profile.ParseFromString(stream)
    if MULTIPLAYER:
        current_user.first_name = profile.first_name
        current_user.last_name = profile.last_name
        db.session.commit()
    return '', 204


@app.route('/api/profiles/<int:player_id>/activities/', methods=['GET', 'POST'], strict_slashes=False)
@jwt_to_session_cookie
@login_required
def api_profiles_activities(player_id):
    if request.method == 'POST':
        if not request.stream:
            return '', 400
        if current_user.player_id != player_id:
            return '', 401
        activity = activity_pb2.Activity()
        activity.ParseFromString(request.stream.read())
        activity.id = get_id('activity')
        insert_protobuf_into_db('activity', activity)
        return '{"id": %ld}' % activity.id, 200

    # request.method == 'GET'
    activities = activity_pb2.Activities()
    # Select every column except 'fit' - despite being a blob python 3 treats it like a utf-8 string and tries to decode it
    rows = db.engine.execute("SELECT id, player_id, f3, name, f5, f6, start_date, end_date, distance, avg_heart_rate, max_heart_rate, avg_watts, max_watts, avg_cadence, max_cadence, avg_speed, max_speed, calories, total_elevation, strava_upload_id, strava_activity_id, f23, fit_filename, f29, date FROM activity WHERE player_id = ?", (str(player_id),))
    for row in rows:
        activity = activities.activities.add()
        row_to_protobuf(row, activity, exclude_fields=['fit'])
        a = activity
        #If all values for saved activity is 0, remove it from DB and results
        #if (a.avg_cadence == 0 and a.avg_heart_rate == 0 and a.avg_speed == 0 and
        #a.avg_watts == 0 and a.calories == 0 and a.distance == 0 and a.max_cadence == 0 and
        #a.max_heart_rate == 0 and a.max_speed == 0 and a.max_watts == 0):
        if a.distance == 0:
            db.engine.execute("DELETE FROM activity WHERE id = %s" % a.id)
            activities.activities.remove(a)

    return activities.SerializeToString(), 200


@app.route('/api/profiles', methods=['GET'])
def api_profiles():
    args = request.args.getlist('id')
    profiles = profile_pb2.Profiles()
    for i in args:
        if int(i) > 10000000:
            # For ghosts
            ghostId = math.floor(int(i) / 10000000)
            player_id = int(i) - ghostId * 10000000
            profile = profile_pb2.Profile()
            profile_file = '%s/%s/profile.bin' % (STORAGE_DIR, str(player_id))
            if os.path.isfile(profile_file):
                with open(profile_file, 'rb') as fd:
                    profile.ParseFromString(fd.read())
            p = profiles.profiles.add()
            p.CopyFrom(profile)
            p.id = int(i)
            p.first_name = 'zoffline'
            p.last_name = 'ghost %s' % ghostId
            p.f20 = 3761002195 # basic 4 jersey
            p.f24 = 1456463855 # tron bike
            p.f27 = 125 # blue
            p.country_code = 0
        else:
            profile = profile_pb2.Profile()
            profile_file = '%s/%s/profile.bin' % (STORAGE_DIR, i)
            if os.path.isfile(profile_file):
                with open(profile_file, 'rb') as fd:
                    profile.ParseFromString(fd.read())
            p = profiles.profiles.add()
            p.CopyFrom(profile)
    return profiles.SerializeToString(), 200


def strava_upload(player_id, activity):
    try:
        from stravalib.client import Client
    except ImportError:
        logger.warn("stravalib is not installed. Skipping Strava upload attempt.")
        return
    profile_dir = '%s/%s' % (STORAGE_DIR, player_id)
    strava = Client()
    try:
        with open('%s/strava_token.txt' % profile_dir, 'r') as f:
            client_id = f.readline().rstrip('\r\n')
            client_secret = f.readline().rstrip('\r\n')
            strava.access_token = f.readline().rstrip('\r\n')
            refresh_token = f.readline().rstrip('\r\n')
            expires_at = f.readline().rstrip('\r\n')
    except:
        logger.warn("Failed to read %s/strava_token.txt. Skipping Strava upload attempt." % profile_dir)
        return
    try:
        if getUTCTime() > int(expires_at):
            refresh_response = strava.refresh_access_token(client_id=client_id, client_secret=client_secret,
                                                           refresh_token=refresh_token)
            with open('%s/strava_token.txt' % profile_dir, 'w') as f:
                f.write(client_id + '\n')
                f.write(client_secret + '\n')
                f.write(refresh_response['access_token'] + '\n')
                f.write(refresh_response['refresh_token'] + '\n')
                f.write(str(refresh_response['expires_at']) + '\n')
    except:
        logger.warn("Failed to refresh token. Skipping Strava upload attempt.")
        return
    try:
        # See if there's internet to upload to Strava
        strava.upload_activity(BytesIO(activity.fit), data_type='fit', name=activity.name)
        # XXX: assume the upload succeeds on strava's end. not checking on it.
    except:
        logger.warn("Strava upload failed. No internet?")


def garmin_upload(player_id, activity):
    try:
        from garmin_uploader.workflow import Workflow
    except ImportError:
        logger.warn("garmin_uploader is not installed. Skipping Garmin upload attempt.")
        return
    profile_dir = '%s/%s' % (STORAGE_DIR, player_id)
    try:
        with open('%s/garmin_credentials.txt' % profile_dir, 'r') as f:
            username = f.readline().rstrip('\r\n')
            password = f.readline().rstrip('\r\n')
    except:
        logger.warn("Failed to read %s/garmin_credentials.txt. Skipping Garmin upload attempt." % profile_dir)
        return
    try:
        with open('%s/last_activity.fit' % profile_dir, 'wb') as f:
            f.write(activity.fit)
    except:
        logger.warn("Failed to save fit file. Skipping Garmin upload attempt.")
        return
    try:
        w = Workflow(['%s/last_activity.fit' % profile_dir], activity_name=activity.name, username=username, password=password)
        w.run()
    except:
        logger.warn("Garmin upload failed. No internet?")


# With 64 bit ids Zwift can pass negative numbers due to overflow, which the flask int
# converter does not handle so it's a string argument
@app.route('/api/profiles/<int:player_id>/activities/<string:activity_id>', methods=['PUT'])
@jwt_to_session_cookie
@login_required
def api_profiles_activities_id(player_id, activity_id):
    if not request.stream:
        return '', 400
    if current_user.player_id != player_id:
        return '', 401
    activity_id = int(activity_id) & 0xffffffffffffffff
    activity = activity_pb2.Activity()
    activity.ParseFromString(request.stream.read())
    update_protobuf_in_db('activity', activity, activity_id)

    response = '{"id":%s}' % activity_id
    if request.args.get('upload-to-strava') != 'true':
        return response, 200
    player_id = current_user.player_id
    if current_user.enable_ghosts:
        try:
            saveGhost(activity.name, int(player_id))
        except:
            pass
    # Unconditionally *try* and upload to strava and garmin since profile may
    # not be properly linked to strava/garmin (i.e. no 'upload-to-strava' call
    # will occur with these profiles).
    strava_upload(player_id, activity)
    garmin_upload(player_id, activity)
    return response, 200

@app.route('/api/profiles/<int:recieving_player_id>/activities/0/rideon', methods=['POST']) #activity_id Seem to always be 0, even when giving ride on to ppl with 30km+
@jwt_to_session_cookie
@login_required
def api_profiles_activities_rideon(recieving_player_id):
    sending_player_id = request.json['profileId']
    profile = getPartialProfile(sending_player_id)
    if not profile == None:
        player_update = udp_node_msgs_pb2.PlayerUpdate()
        player_update.f2 = 1
        player_update.type = 4 #ride on type
        player_update.world_time1 = world_time()
        player_update.world_time2 = player_update.world_time1 + 9890
        player_update.f14 = int(getUTCTime() * 1000000)

        ride_on = udp_node_msgs_pb2.RideOn()
        ride_on.rider_id = int(sending_player_id)
        ride_on.to_rider_id = int(recieving_player_id)
        ride_on.firstName = profile.first_name
        ride_on.lastName = profile.last_name
        ride_on.countryCode = profile.country_code

        player_update.payload = ride_on.SerializeToString()

        if not recieving_player_id in playerUpdateQueue:
            playerUpdateQueue[recieving_player_id] = list()
        playerUpdateQueue[recieving_player_id].append(player_update.SerializeToString())
    return '{}', 200


@app.route('/api/profiles/<int:player_id>/followees', methods=['GET'])
def api_profiles_followees(player_id):
    return '', 200


def get_week_range(dt):
     d = datetime.datetime(dt.year,1,1)
     if (d.weekday()<= 3):
         d = d - timedelta(d.weekday())
     else:
         d = d + timedelta(7-d.weekday())
     dlt = timedelta(days = (int(dt.strftime('%W'))-1)*7)
     first = d + dlt
     last = d + dlt + timedelta(days=6, hours=23, minutes=59, seconds=59)
     return first, last

def get_month_range(dt):
     num_days = calendar.monthrange(dt.year, dt.month)[1]
     first = datetime.datetime(dt.year, dt.month, 1)
     last = datetime.datetime(dt.year, dt.month, num_days, 23, 59, 59)
     return first, last


def unix_time_millis(dt):
    return int(dt.timestamp()*1000)


def fill_in_goal_progress(goal, player_id):
    now = getUTCDateTime()
    if goal.periodicity == 0:  # weekly
        first_dt, last_dt = get_week_range(now)
    else:  # monthly
        first_dt, last_dt = get_month_range(now)
    if goal.type == 0:  # distance
        distance = db.engine.execute("""SELECT SUM(distance) FROM activity
                       WHERE player_id = ?
                       AND strftime('%s', start_date) >= strftime('%s', ?)
                       AND strftime('%s', start_date) <= strftime('%s', ?)
                       AND end_date IS NOT NULL""",
                       (str(player_id), first_dt, last_dt)).first()[0]
        if distance:
            goal.actual_distance = distance
            goal.actual_duration = distance
        else:
            goal.actual_distance = 0.0
            goal.actual_duration = 0.0

    else:  # duration
        duration = db.engine.execute("""SELECT SUM(julianday(end_date) - julianday(start_date))
                       FROM activity
                       WHERE player_id = ?
                       AND strftime('%s', start_date) >= strftime('%s', ?)
                       AND strftime('%s', start_date) <= strftime('%s', ?)
                       AND end_date IS NOT NULL""",
                       (str(player_id), first_dt, last_dt)).first()[0]
        if duration:
            goal.actual_duration = duration*1440  # convert from days to minutes
            goal.actual_distance = duration*1440
        else:
            goal.actual_duration = 0.0
            goal.actual_distance = 0.0


def set_goal_end_date(goal, now):
    if goal.periodicity == 0:  # weekly
        goal.period_end_date = unix_time_millis(get_week_range(now)[1])
    else:  # monthly
        goal.period_end_date = unix_time_millis(get_month_range(now)[1])


@app.route('/api/profiles/<int:player_id>/goals', methods=['GET', 'POST'])
@jwt_to_session_cookie
@login_required
def api_profiles_goals(player_id):
    if player_id != current_user.player_id:
        return '', 401
    if request.method == 'POST':
        if not request.stream:
            return '', 400
        goal = goal_pb2.Goal()
        goal.ParseFromString(request.stream.read())
        goal.id = get_id('goal')
        now = getUTCDateTime()
        goal.created_on = unix_time_millis(now)
        set_goal_end_date(goal, now)
        fill_in_goal_progress(goal, player_id)
        insert_protobuf_into_db('goal', goal)

        return goal.SerializeToString(), 200

    # request.method == 'GET'
    goals = goal_pb2.Goals()
    rows = db.engine.execute("SELECT * FROM goal WHERE player_id = ?", (str(player_id),))
    for row in rows:
        goal = goals.goals.add()
        row_to_protobuf(row, goal)
        end_dt = datetime.datetime.fromtimestamp(goal.period_end_date / 1000)
        now = getUTCDateTime()
        if end_dt < now:
            set_goal_end_date(goal, now)
            update_protobuf_in_db('goal', goal, goal.id)
        fill_in_goal_progress(goal, player_id)

    return goals.SerializeToString(), 200


@app.route('/api/profiles/<int:player_id>/goals/<string:goal_id>', methods=['DELETE'])
@jwt_to_session_cookie
@login_required
def api_profiles_goals_id(player_id, goal_id):
    if player_id != current_user.player_id:
        return '', 401
    goal_id = int(goal_id) & 0xffffffffffffffff
    db.engine.execute("DELETE FROM goal WHERE id = ?", (str(goal_id),))
    return '', 200


@app.route('/api/tcp-config', methods=['GET'])
def api_tcp_config():
    infos = periodic_info_pb2.PeriodicInfos()
    info = infos.infos.add()
    if os.path.exists(SERVER_IP_FILE):
        with open(SERVER_IP_FILE, 'r') as f:
            info.game_server_ip = f.read().rstrip('\r\n')
    else:
        info.game_server_ip = '127.0.0.1'
    info.f2 = 3023
    return infos.SerializeToString(), 200


def relay_worlds_generic(world_id=None):
    courses = coursesLookup.keys()
    # Android client also requests a JSON version
    if request.headers['Accept'] == 'application/json':
        if request.content_type == 'application/x-protobuf-lite':
            #chat_message = udp_node_msgs_pb2.ChatMessage()
            #serializedMessage = None
            try:
                player_update = udp_node_msgs_pb2.PlayerUpdate()
                player_update.ParseFromString(request.data)
                #chat_message.ParseFromString(request.data[6:])
                #serializedMessage = chat_message.SerializeToString()
            except:
                #Not able to decode as playerupdate, send dummy response
                world = { 'currentDateTime': int(getUTCTime()),
                        'currentWorldTime': world_time(),
                        'friendsInWorld': [],
                        'mapId': 1,
                        'name': 'Public Watopia',
                        'playerCount': 0,
                        'worldId': 1
                        }
                if world_id:
                    world['mapId'] = world_id
                    return jsonify(world)
                else:
                    return jsonify([ world ])

            #PlayerUpdate
            player_update.world_time2 = world_time() + 60000
            player_update.f12 = 1
            player_update.f14 = int(str(int(getUTCTime()*1000000)))
            for recieving_player_id in online.keys():
                should_receive = False
                if player_update.type == 5 or player_update.type == 105:
                    recieving_player = online[recieving_player_id]
                    #Chat message
                    if player_update.type == 5:
                        chat_message = udp_node_msgs_pb2.ChatMessage()
                        chat_message.ParseFromString(player_update.payload)
                        sending_player_id = chat_message.rider_id
                        if sending_player_id in online:
                            sending_player = online[sending_player_id]
                            #Check that players are on same course and close to each other
                            if isNearby(sending_player, recieving_player):
                                should_receive = True
                    #Segment complete
                    else:
                        segment_complete = udp_node_msgs_pb2.SegmentComplete()
                        segment_complete.ParseFromString(player_update.payload)
                        sending_player_id = segment_complete.rider_id
                        if sending_player_id in online:
                            sending_player = online[sending_player_id]
                            #Check that players are on same course and close to each other
                            if getCourse(sending_player) == getCourse(recieving_player):
                                should_receive = True
                #Other PlayerUpdate, send to all
                else:
                    should_receive = True
                if should_receive:
                    if not recieving_player_id in playerUpdateQueue:
                        playerUpdateQueue[recieving_player_id] = list()
                    playerUpdateQueue[recieving_player_id].append(player_update.SerializeToString())
            return '{}', 200
    else:  # protobuf request
        worlds = world_pb2.Worlds()
        world = None

        for course in courses:
            world = worlds.worlds.add()
            world.id = 1
            world.name = 'Public Watopia'
            world.f3 = course
            world.world_time = world_time()
            world.real_time = int(getUTCTime())
            playersInRegion = 0
            for p_id in online.keys():
                player = online[p_id]
                courseId = getCourse(player)
                if course == courseId:
                    partialProfile = getPartialProfile(player.id)
                    if not partialProfile == None:
                        onlinePlayer = world.player_states.add()
                        onlinePlayer.id = player.id
                        onlinePlayer.firstName = partialProfile.first_name
                        onlinePlayer.lastName = partialProfile.last_name
                        onlinePlayer.distance = player.distance
                        onlinePlayer.time = player.time
                        onlinePlayer.f6 = 840#0
                        onlinePlayer.f8 = 0
                        onlinePlayer.f9 = 0
                        onlinePlayer.f10 = 0
                        onlinePlayer.f11 = 0
                        onlinePlayer.power = 250#player.power
                        onlinePlayer.f13 = 2355
                        onlinePlayer.x = player.x
                        onlinePlayer.altitude = player.altitude
                        onlinePlayer.y = player.y
                        playersInRegion += 1
            world.f5 = playersInRegion
        if world_id:
            world.id = world_id
            return world.SerializeToString()
        else:
            return worlds.SerializeToString()


@app.route('/relay/worlds', methods=['GET'])
@app.route('/relay/dropin', methods=['GET'])
def relay_worlds():
    return relay_worlds_generic()


@app.route('/relay/worlds/<int:world_id>', methods=['GET'])
def relay_worlds_id(world_id):
    return relay_worlds_generic(world_id)


@app.route('/relay/worlds/<int:world_id>/join', methods=['POST'])
def relay_worlds_id_join(world_id):
    return '{"worldTime":%ld}' % world_time()


@app.route('/relay/worlds/<int:world_id>/players/<int:player_id>', methods=['GET'])
def relay_worlds_id_players_id(world_id, player_id):
    for p_id in online.keys():
        player = online[p_id]
        if player.id == player_id:
            return player.SerializeToString()
    return None


@app.route('/relay/worlds/<int:world_id>/my-hash-seeds', methods=['GET'])
def relay_worlds_my_hash_seeds(world_id):
    return '[{"expiryDate":196859639979,"seed1":-733221030,"seed2":-2142448243},{"expiryDate":196860425476,"seed1":1528095532,"seed2":-2078218472},{"expiryDate":196862212008,"seed1":1794747796,"seed2":-1901929955},{"expiryDate":196862637148,"seed1":-1411883466,"seed2":1171710140},{"expiryDate":196863874267,"seed1":670195825,"seed2":-317830991}]'


@app.route('/relay/worlds/hash-seeds', methods=['GET'])
def relay_worlds_hash_seeds():
    seeds = hash_seeds_pb2.HashSeeds()
    for x in range(4):
        seed = seeds.seeds.add()
        seed.seed1 = int(random.getrandbits(31))
        seed.seed2 = int(random.getrandbits(31))
        seed.expiryDate = world_time()+(10800+x*1200)*1000
    return seeds.SerializeToString(), 200


# XXX: attributes have not been thoroughly investigated
@app.route('/relay/worlds/<int:world_id>/attributes', methods=['POST'])
def relay_worlds_attributes(world_id):
# NOTE: This was previously a protobuf message in Zwift client, but later changed.
#    attribs = world_pb2.WorldAttributes()
#    attribs.world_time = world_time()
#    return attribs.SerializeToString(), 200
    return relay_worlds_generic(world_id)


@app.route('/relay/periodic-info', methods=['GET'])
def relay_periodic_info():
    infos = periodic_info_pb2.PeriodicInfos()
    info = infos.infos.add()
    if os.path.exists(SERVER_IP_FILE):
        with open(SERVER_IP_FILE, 'r') as f:
            info.game_server_ip = f.read().rstrip('\r\n')
    else:
        info.game_server_ip = '127.0.0.1'
    info.f2 = 3022
    info.f3 = 10
    info.f4 = 60
    info.f5 = 30
    info.f6 = 3
    return infos.SerializeToString(), 200


def add_segment_results(segment_id, player_id, only_best, from_date, to_date, results):
    where_stmt = "WHERE segment_id = ?"
    where_args = [str(segment_id)]
    rows = None
    if player_id:
        where_stmt += " AND player_id = ?"
        where_args.append(player_id)
    if from_date:
        where_stmt += " AND strftime('%s', finish_time_str) > strftime('%s', ?)"
        where_args.append(from_date)
    if to_date:
        where_stmt += " AND strftime('%s', finish_time_str) < strftime('%s', ?)"
        where_args.append(to_date)
    if only_best:
        where_stmt += " AND world_time > ?"
        #Only include results from max 1 hour ago
        where_args.append(world_time()-(60*60*1000))
        rows = db.engine.execute("""SELECT s1.* FROM segment_result s1
                        JOIN (SELECT s.player_id, MIN(Cast(s.elapsed_ms AS INTEGER)) AS min_time
                            FROM segment_result s %s GROUP BY s.player_id) s2 ON s2.player_id = s1.player_id AND s2.min_time = CAST(s1.elapsed_ms AS INTEGER)
                        GROUP BY s1.player_id, s1.elapsed_ms
                        ORDER BY CAST(s1.elapsed_ms AS INTEGER)
                        LIMIT 1000""" % where_stmt, where_args)
    else:
        rows = db.engine.execute("SELECT * FROM segment_result %s" % where_stmt, where_args)
    for row in rows:
        result = results.segment_results.add()
        row_to_protobuf(row, result, ['f3', 'f4', 'segment_id', 'event_subgroup_id', 'finish_time_str', 'f14', 'f17', 'f18'])

def handle_segment_results(request):
    if request.method == 'POST':
        if not request.stream:
            return '', 400
        result = segment_result_pb2.SegmentResult()
        result.ParseFromString(request.stream.read())
        result.id = get_id('segment_result')
        result.world_time = world_time()
        result.finish_time_str = getUTCDateTime().strftime("%Y-%m-%dT%H:%M:%SZ")
        result.f20 = 0
        insert_protobuf_into_db('segment_result', result)
        return '{"id": %ld}' % result.id, 200

    # request.method == GET
#    world_id = int(request.args.get('world_id'))
    player_id = request.args.get('player_id')
#    full = request.args.get('full') == 'true'
    # Require segment_id
    if not request.args.get('segment_id'):
        return '', 422
    segment_id = int(request.args.get('segment_id')) & 0xffffffffffffffff
    only_best = request.args.get('only-best') == 'true'
    from_date = request.args.get('from')
    to_date = request.args.get('to')

    results = segment_result_pb2.SegmentResults()
    results.world_id = 1
    results.segment_id = segment_id

    if player_id:
        #Add players results
        add_segment_results(segment_id, player_id, only_best, from_date, to_date, results)
    else:
        #Top 100 results, player_id = None
        add_segment_results(segment_id, player_id, only_best, from_date, to_date, results)

    return results.SerializeToString(), 200


@app.route('/relay/segment-results', methods=['GET'])
def relay_segment_results():
    return handle_segment_results(request)


@app.route('/api/segment-results', methods=['GET', 'POST'])
@jwt_to_session_cookie
@login_required
def api_segment_results():
    #Checks that online player has values for ghosts and player_id
    player_id = current_user.player_id
    if request.method == 'POST' and player_id != current_user.player_id:
        return '', 401
    return handle_segment_results(request)


@app.route('/relay/worlds/<int:world_id>/leave', methods=['POST'])
def relay_worlds_leave(world_id):
    return '{"worldtime":%ld}' % world_time()


def connect_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.text_factory = str
    conn.row_factory = sqlite3.Row
    return conn


@app.teardown_request
def teardown_request(exception):
    if exception != None:
        print('Exception: %s' % exception)


def init_database():
    if not os.path.exists(DATABASE_PATH) or not os.path.getsize(DATABASE_PATH):
        # Create a new database
        with open(DATABASE_INIT_SQL, 'r') as f:
            db.engine.execute(f.read())
            db.engine.execute('INSERT INTO version VALUES (?)', (DATABASE_CUR_VER,))
        return
    # Migrate database if necessary
    if not os.access(DATABASE_PATH, os.W_OK):
        logging.error("zwift-offline.db is not writable. Unable to upgrade database!")
        return
    version = db.engine.execute('SELECT version FROM version').first()[0]
    if version == DATABASE_CUR_VER:
        return
    # Database needs to be upgraded, try to back it up first
    try:  # Try writing to storage dir
        copyfile(DATABASE_PATH, "%s.v%d.%d.bak" % (DATABASE_PATH, version, int(getUTCTime())))
    except:
        try:  # Fall back to a temporary dir
            copyfile(DATABASE_PATH, "%s/zwift-offline.db.v%s.%d.bak" % (tempfile.gettempdir(), version, int(getUTCTime())))
        except:
            logging.warn("Failed to create a zoffline database backup prior to upgrading it.")

    if version < 1:
        # Adjust old world_time values in segment results to new rough estimate of Zwift's
        logging.info("Upgrading zwift-offline.db to version 2")
        db.engine.execute('UPDATE segment_result SET world_time = world_time-1414016075000')
        db.engine.execute('UPDATE version SET version = 2')

    if version == 1:
        logging.info("Upgrading zwift-offline.db to version 2")
        db.engine.execute('UPDATE segment_result SET world_time = cast(world_time/64.4131403573055-1414016075 as int)*1000')
        db.engine.execute('UPDATE version SET version = 2')


def check_columns():
    time.sleep(3)
    rows = db.engine.execute(sqlalchemy.text("PRAGMA table_info(user)"))
    should_have_columns = User.metadata.tables['user'].columns
    current_columns = list()
    for row in rows:
        current_columns.append(row[1])
    for column in should_have_columns:
        if not column.name in current_columns:
            nulltext = None
            if column.nullable:
                nulltext = "NULL"
            else:
                nulltext = "NOT NULL"
            defaulttext = None
            if column.default == None:
                defaulttext = ""
            else:
                defaulttext = " DEFAULT %s" % column.default.arg
            db.engine.execute(sqlalchemy.text("ALTER TABLE user ADD %s %s %s%s;" % (column.name, str(column.type), nulltext, defaulttext)))


@app.before_first_request
def before_first_request():
    init_database()
    check_columns()
    db.create_all()


####################
#
# Auth server (secure.zwift.com) routes below here
#
####################

@app.route('/auth/rb_bf03269xbi', methods=['POST'])
def auth_rb():
    return 'OK(Java)'


@app.route('/launcher', methods=['GET'])
@app.route('/launcher/realms/zwift/protocol/openid-connect/auth', methods=['GET'])
@app.route('/launcher/realms/zwift/protocol/openid-connect/registrations', methods=['GET'])
@app.route('/auth/realms/zwift/protocol/openid-connect/auth', methods=['GET'])
@app.route('/auth/realms/zwift/login-actions/request/login', methods=['GET', 'POST'])
@app.route('/auth/realms/zwift/protocol/openid-connect/registrations', methods=['GET'])
@app.route('/auth/realms/zwift/login-actions/startriding', methods=['GET'])  # Unused as it's a direct redirect now from auth/login
@app.route('/auth/realms/zwift/tokens/login', methods=['GET'])  # Called by Mac, but not Windows
@app.route('/auth/realms/zwift/tokens/registrations', methods=['GET'])  # Called by Mac, but not Windows
@app.route('/ride', methods=['GET'])
def launch_zwift():
    # Zwift client has switched to calling https://launcher.zwift.com/launcher/ride
    if request.path != "/ride" and not os.path.exists(AUTOLAUNCH_FILE):
        if MULTIPLAYER:
            return render_template("login_form.html")
        else:
            return render_template("user_home.html", username="", enable_ghosts=False, online=getOnline())
    else:
        if MULTIPLAYER:
            return redirect("http://zwift/?code=zwift_refresh_token%s" % fake_refresh_token_with_session_cookie(request.cookies.get('remember_token')), 302)
        else:
            return redirect("http://zwift/?code=zwift_refresh_token%s" % REFRESH_TOKEN, 302)


def fake_refresh_token_with_session_cookie(session_cookie):
    refresh_token = jwt.decode(REFRESH_TOKEN, options=({'verify_signature': False, 'verify_aud': False}))
    refresh_token['session_cookie'] = session_cookie
    refresh_token = jwt.encode(refresh_token, 'nosecret').decode('utf-8')
    return refresh_token


def fake_jwt_with_session_cookie(session_cookie):
    access_token = jwt.decode(ACCESS_TOKEN, options=({'verify_signature': False, 'verify_aud': False}))
    access_token['session_cookie'] = session_cookie
    access_token = jwt.encode(access_token, 'nosecret').decode('utf-8')

    refresh_token = fake_refresh_token_with_session_cookie(session_cookie)

    return """{"access_token":"%s","expires_in":1000021600,"refresh_expires_in":611975560,"refresh_token":"%s","token_type":"bearer","id_token":"%s","not-before-policy":1408478984,"session_state":"0846ab9a-765d-4c3f-a20c-6cac9e86e5f3","scope":""}""" % (access_token, refresh_token, ID_TOKEN)


@app.route('/auth/realms/zwift/protocol/openid-connect/token', methods=['POST'])
def auth_realms_zwift_protocol_openid_connect_token():
    # Android client login
    username = request.form.get('username')
    password = request.form.get('password')

    if username and MULTIPLAYER:
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.pass_hash, password):
            login_user(user, remember=True)
        else:
            return '', 401

    if MULTIPLAYER:
        # This is called once with ?code= in URL and once again with the refresh token
        if "code" in request.form:
            # Original code argument is replaced with session cookie from launcher
            refresh_token = jwt.decode(request.form['code'][19:], options=({'verify_signature': False, 'verify_aud': False}))
            session_cookie = refresh_token['session_cookie']
            return fake_jwt_with_session_cookie(session_cookie), 200
        elif "refresh_token" in request.form:
            token = jwt.decode(request.form['refresh_token'], options=({'verify_signature': False, 'verify_aud': False}))
            return fake_jwt_with_session_cookie(token['session_cookie'])
        else:  # android login
            from flask_login import encode_cookie
            # cookie is not set in request since we just logged in so create it.
            return fake_jwt_with_session_cookie(encode_cookie(str(session['_user_id']))), 200
    else:
        return FAKE_JWT, 200

@app.route("/start-zwift" , methods=['POST'])
@login_required
def start_zwift():
    if MULTIPLAYER:
        current_user.enable_ghosts = 'enableghosts' in request.form.keys()
        ghostsEnabled[current_user.player_id] = current_user.enable_ghosts
    else:
        AnonUser.enable_ghosts = 'enableghosts' in request.form.keys()
    db.session.commit()
    selected_map = request.form['map']
    if selected_map == 'CALENDAR':
        return redirect("/ride", 302)
    else:
        response = make_response(redirect("http://cdn.zwift.com/%s" % selected_map, 302))
        response.set_cookie('remember_token', request.cookies['remember_token'], domain=".zwift.com")
        return response


# Called by Mac, but not Windows
@app.route('/auth/realms/zwift/tokens/access/codes', methods=['POST'])
def auth_realms_zwift_tokens_access_codes():
    if MULTIPLAYER:
        if "code" in request.form:
            remember_token = unquote(request.form['code'])
            return fake_jwt_with_session_cookie(remember_token), 200
        elif "refresh_token" in request.form:
            token = jwt.decode(request.form['refresh_token'], options=({'verify_signature': False, 'verify_aud': False}))
            return fake_jwt_with_session_cookie(token['session_cookie'])
        remember_token = unquote(request.form['code'])
        return fake_jwt_with_session_cookie(remember_token), 200
    else:
        return FAKE_JWT, 200


@app.route('/static/web/launcher/<filename>', methods=['GET'])
def static_web_launcher(filename):
    return render_template(filename)


def run_standalone(passedOnline, passedGhostsEnabled, passedSaveGhost, passedPlayerUpdateQueue):
    global online
    global ghostsEnabled
    global saveGhost
    global playerUpdateQueue
    global login_manager
    online = passedOnline
    ghostsEnabled = passedGhostsEnabled
    saveGhost = passedSaveGhost
    playerUpdateQueue = passedPlayerUpdateQueue
    login_manager = LoginManager()
    login_manager.login_view = 'login'
    login_manager.session_protection = None
    if not MULTIPLAYER:
        login_manager.anonymous_user = AnonUser
    login_manager.init_app(app)
    db.create_all(app=app)
    db.session.commit()

    @login_manager.user_loader
    def load_user(uid):
        return User.query.get(int(uid))

    app.run(ssl_context=('%s/cert-zwift-com.pem' % SSL_DIR, '%s/key-zwift-com.pem' % SSL_DIR), port=443, threaded=True, host='0.0.0.0') # debug=True, use_reload=False)


if __name__ == "__main__":
    run_standalone({}, {}, None)
