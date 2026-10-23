"""Small, dependency-free CANopen EDS parser and typed value codec.

EDS metadata describes object types/access, but does not establish hardware safety,
permissions actually granted by firmware, or Sevcon-specific engineering scaling.
"""
from __future__ import annotations

import configparser
import re
import struct
from dataclasses import dataclass
from pathlib import Path

# CiA 301 data types supported by expedited (<=4-byte) SDO transfer.
TYPES = {
    0x0001: ('BOOLEAN', 'B', 1),
    0x0002: ('INTEGER8', 'b', 1),
    0x0003: ('INTEGER16', 'h', 2),
    0x0004: ('INTEGER32', 'i', 4),
    0x0005: ('UNSIGNED8', 'B', 1),
    0x0006: ('UNSIGNED16', 'H', 2),
    0x0007: ('UNSIGNED32', 'I', 4),
    0x0008: ('REAL32', 'f', 4),
}

@dataclass(frozen=True)
class Entry:
    index: int
    subindex: int
    name: str
    data_type: int
    access: str
    low: str = ''
    high: str = ''
    default: str = ''
    pdo: str = ''

    @property
    def label(self):
        return TYPES.get(self.data_type, (f'0x{self.data_type:04X}', '', 0))[0]

    @property
    def readable(self):
        return self.access in ('ro', 'rw', 'rwr', 'rww', 'const')

    @property
    def writable(self):
        return self.access in ('rw', 'wo', 'rwr', 'rww')

    @property
    def expedited(self):
        return self.data_type in TYPES


def number(s, default=0):
    s = str(s).strip().replace('$NODEID', '0')
    if not s:
        return default
    return int(s, 0) if s.lower().startswith(('0x', '0b')) else int(s)


def load_eds(path):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str.lower
    with open(path, 'r', encoding='utf-8-sig', errors='replace') as stream:
        config.read_file(stream)
    result = []
    # Parameter sections are [2000] or [2000sub1], not [2000Name].
    for sec in config.sections():
        match = re.fullmatch(r'([0-9a-fA-F]{4})(?:sub([0-9a-fA-F]+))?', sec, re.IGNORECASE)
        if not match:
            continue
        d = config[sec]
        # Compound parent records are directory entries, not automatically SDO subindex 0.
        if match.group(2) is None and number(d.get('objecttype', '7')) in (8, 9):
            continue
        i, s = int(match.group(1), 16), int(match.group(2), 16) if match.group(2) else 0
        try:
            dtype = number(d.get('datatype', '0'))
        except ValueError:
            dtype = 0
        result.append(Entry(i, s, d.get('parametername', sec), dtype,
                            d.get('accesstype', 'ro').lower().strip(),
                            d.get('lowlimit', ''), d.get('highlimit', ''),
                            d.get('defaultvalue', ''), d.get('pdomapping', '')))
    if not result:
        raise ValueError('No CANopen object entries found in the selected EDS file')
    return sorted(result, key=lambda x: (x.index, x.subindex))


def decode(entry, raw):
    if entry.data_type not in TYPES:
        return raw.hex(' ').upper() + ' (raw; unsupported EDS type)'
    name, fmt, n = TYPES[entry.data_type]
    if len(raw) != n:
        return raw.hex(' ').upper() + f' (length mismatch: expected {n})'
    val = struct.unpack('<' + fmt, raw)[0]
    return str(bool(val)) if name == 'BOOLEAN' else str(val)


def encode(entry, value):
    if entry.data_type not in TYPES:
        raise ValueError('This EDS data type is not supported for writing')
    name, fmt, n = TYPES[entry.data_type]
    if name == 'BOOLEAN':
        v = value.strip().lower()
        if v not in ('0', '1', 'true', 'false'):
            raise ValueError('Enter true/false or 1/0')
        parsed = int(v in ('1', 'true'))
    elif name == 'REAL32':
        parsed = float(value)
        import math
        if not math.isfinite(parsed):
            raise ValueError('A finite number is required')
    else:
        parsed = int(value.strip(), 0) if value.strip().lower().startswith(('0x', '0b')) else int(value.strip(), 10)
    for limit, label in ((entry.low, 'minimum'), (entry.high, 'maximum')):
        if limit and '$' not in limit:
            try:
                bound = float(number(limit)) if name != 'REAL32' else float(limit)
                if label == 'minimum' and parsed < bound or label == 'maximum' and parsed > bound:
                    raise ValueError(f'Value violates EDS {label}: {limit}')
            except ValueError as e:
                if str(e).startswith('Value violates'):
                    raise
                # Ignore symbolic or unparseable EDS limits; do not invent a bound.
    try:
        return struct.pack('<' + fmt, parsed)
    except (struct.error, OverflowError) as e:
        raise ValueError('Value is out of range for ' + name) from e