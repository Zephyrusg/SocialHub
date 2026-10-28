"""Bounded ISO BMFF box inspection, without decoding or reading media payloads."""
import struct


def mp4_layout(path):
    count, positions, edit_list = 0, {}, False
    with path.open('rb') as stream:
        stream.seek(0, 2)
        size = stream.tell()
        def walk(start, end, depth=0):
            nonlocal count, edit_list
            position = start
            while position < end:
                count += 1
                if count > 4096 or depth > 8 or end - position < 8:
                    raise ValueError('Invalid MP4 box layout')
                stream.seek(position)
                length, kind = struct.unpack('>I4s', stream.read(8))
                header = 8
                if length == 1:
                    raw = stream.read(8)
                    if len(raw) != 8: raise ValueError('Truncated extended box')
                    length, header = struct.unpack('>Q', raw)[0], 16
                if length == 0: length = end - position
                if length < header or position + length > end:
                    raise ValueError('Truncated MP4 box')
                if depth == 0:
                    if kind == b'moov' and kind in positions: raise ValueError('Duplicate movie box')
                    positions.setdefault(kind, position)
                if kind in {b'edts', b'elst'}: edit_list = True
                if kind in {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts'}:
                    walk(position + header, position + length, depth + 1)
                position += length
        walk(0, size)
    return {'fast_start': b'moov' in positions and b'mdat' in positions and positions[b'moov'] < positions[b'mdat'],
            'has_edit_list': edit_list}
