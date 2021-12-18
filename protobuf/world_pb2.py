# -*- coding: utf-8 -*-
# Generated by the protocol buffer compiler.  DO NOT EDIT!
# source: world.proto
"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import message as _message
from google.protobuf import reflection as _reflection
from google.protobuf import symbol_database as _symbol_database
# @@protoc_insertion_point(imports)

_sym_db = _symbol_database.Default()




DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n\x0bworld.proto\"\xa6\x01\n\x05World\x12\n\n\x02id\x18\x01 \x02(\r\x12\x0c\n\x04name\x18\x02 \x02(\t\x12\n\n\x02\x66\x33\x18\x03 \x02(\r\x12\n\n\x02\x66\x35\x18\x05 \x02(\x04\x12\x12\n\nworld_time\x18\x06 \x02(\x04\x12\x11\n\treal_time\x18\x07 \x02(\x04\x12\x1e\n\rplayer_states\x18\x08 \x03(\x0b\x32\x07.Player\x12$\n\x13pace_partner_states\x18\x0c \x03(\x0b\x32\x07.Player\" \n\x06Worlds\x12\x16\n\x06worlds\x18\x01 \x03(\x0b\x32\x06.World\"%\n\x0fWorldAttributes\x12\x12\n\nworld_time\x18\x02 \x02(\x03\"\xf7\x01\n\x06Player\x12\n\n\x02id\x18\x01 \x02(\r\x12\x11\n\tfirstName\x18\x02 \x02(\t\x12\x10\n\x08lastName\x18\x03 \x02(\t\x12\x10\n\x08\x64istance\x18\x04 \x01(\r\x12\x0c\n\x04time\x18\x05 \x01(\r\x12\n\n\x02\x66\x36\x18\x06 \x01(\r\x12\n\n\x02\x66\x38\x18\x08 \x01(\r\x12\n\n\x02\x66\x39\x18\t \x01(\r\x12\x0b\n\x03\x66\x31\x30\x18\n \x01(\r\x12\x0b\n\x03\x66\x31\x31\x18\x0b \x01(\r\x12\r\n\x05power\x18\x0c \x01(\r\x12\x0b\n\x03\x66\x31\x33\x18\r \x01(\r\x12\t\n\x01x\x18\x0e \x01(\x02\x12\x10\n\x08\x61ltitude\x18\x0f \x01(\x02\x12\t\n\x01y\x18\x10 \x01(\x02\x12\r\n\x05route\x18\x11 \x01(\x04\x12\x0b\n\x03\x66\x31\x38\x18\x12 \x01(\r')



_WORLD = DESCRIPTOR.message_types_by_name['World']
_WORLDS = DESCRIPTOR.message_types_by_name['Worlds']
_WORLDATTRIBUTES = DESCRIPTOR.message_types_by_name['WorldAttributes']
_PLAYER = DESCRIPTOR.message_types_by_name['Player']
World = _reflection.GeneratedProtocolMessageType('World', (_message.Message,), {
  'DESCRIPTOR' : _WORLD,
  '__module__' : 'world_pb2'
  # @@protoc_insertion_point(class_scope:World)
  })
_sym_db.RegisterMessage(World)

Worlds = _reflection.GeneratedProtocolMessageType('Worlds', (_message.Message,), {
  'DESCRIPTOR' : _WORLDS,
  '__module__' : 'world_pb2'
  # @@protoc_insertion_point(class_scope:Worlds)
  })
_sym_db.RegisterMessage(Worlds)

WorldAttributes = _reflection.GeneratedProtocolMessageType('WorldAttributes', (_message.Message,), {
  'DESCRIPTOR' : _WORLDATTRIBUTES,
  '__module__' : 'world_pb2'
  # @@protoc_insertion_point(class_scope:WorldAttributes)
  })
_sym_db.RegisterMessage(WorldAttributes)

Player = _reflection.GeneratedProtocolMessageType('Player', (_message.Message,), {
  'DESCRIPTOR' : _PLAYER,
  '__module__' : 'world_pb2'
  # @@protoc_insertion_point(class_scope:Player)
  })
_sym_db.RegisterMessage(Player)

if _descriptor._USE_C_DESCRIPTORS == False:

  DESCRIPTOR._options = None
  _WORLD._serialized_start=16
  _WORLD._serialized_end=182
  _WORLDS._serialized_start=184
  _WORLDS._serialized_end=216
  _WORLDATTRIBUTES._serialized_start=218
  _WORLDATTRIBUTES._serialized_end=255
  _PLAYER._serialized_start=258
  _PLAYER._serialized_end=505
# @@protoc_insertion_point(module_scope)
