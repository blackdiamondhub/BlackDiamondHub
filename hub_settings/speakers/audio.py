"""Just enough MP3/WAV parsing to time a clip and join chime + speech into one MP3.

No decoding: MP3 frames are copied as they are. Two MP3s join cleanly when
they share MPEG version, layer, sample rate and channel count (the hub's
chimes are 24 kHz mono, like Google's and Azure's speech); frame bitrates may
differ. Tags (ID3v2, ID3v1, APE) and the Xing/Info/VBRI header frame are
dropped, so the joined file has no stale length header.
"""
import struct
from dataclasses import dataclass

MP3, WAV = "mp3", "wav"
CONTENT_TYPES = {MP3: "audio/mpeg", WAV: "audio/wav"}

# [version][layer] -> kbps by index; version 1 = MPEG-1, 2 = MPEG-2/2.5.
_BITRATES = {
    (1, 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
    (1, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
    (1, 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    (2, 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_BITRATES[(2, 3)] = _BITRATES[(2, 2)]
_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}  # by version bits


class AudioError(ValueError):
    """Not an MP3 or WAV this module can read."""


@dataclass(frozen=True)
class Frame:
    version: int  # version bits: 3 MPEG-1, 2 MPEG-2, 0 MPEG-2.5
    layer: int  # 1, 2 or 3
    bitrate: int  # kbps
    rate: int
    channels: int
    length: int
    samples: int


def _frame(data, i):
    """The MPEG audio frame header at ``data[i]``, or None."""
    if i + 4 > len(data) or data[i] != 0xFF or data[i + 1] & 0xE0 != 0xE0:
        return None
    b1, b2, b3 = data[i + 1], data[i + 2], data[i + 3]
    version_bits, layer_bits = (b1 >> 3) & 3, (b1 >> 1) & 3
    bitrate_idx, rate_idx, padding = b2 >> 4, (b2 >> 2) & 3, (b2 >> 1) & 1
    if version_bits == 1 or layer_bits == 0 or bitrate_idx in (0, 15) or rate_idx == 3:
        return None
    layer = 4 - layer_bits
    family = 1 if version_bits == 3 else 2
    bitrate = _BITRATES[(family, layer)][bitrate_idx]
    rate = _RATES[version_bits][rate_idx]
    if layer == 1:
        samples, length = 384, (12 * bitrate * 1000 // rate + padding) * 4
    elif layer == 2 or family == 1:
        samples, length = 1152, 144 * bitrate * 1000 // rate + padding
    else:
        samples, length = 576, 72 * bitrate * 1000 // rate + padding
    return Frame(version_bits, layer, bitrate, rate, 1 if b3 >> 6 == 3 else 2, length, samples)


def _is_info_frame(data, i, f):
    """True for a Xing/Info/VBRI header frame (a silent frame describing the file)."""
    if f.layer != 3:
        return False
    side = (17 if f.channels == 1 else 32) if f.version == 3 else (9 if f.channels == 1 else 17)
    return data[i + 4 + side:i + 8 + side] in (b"Xing", b"Info") or data[i + 36:i + 40] == b"VBRI"


def _id3v2_end(data):
    i = 0
    while data[i:i + 3] == b"ID3" and len(data) >= i + 10:
        size = 0
        for b in data[i + 6:i + 10]:
            size = (size << 7) | (b & 0x7F)
        i += 10 + size + (10 if data[i + 5] & 0x10 else 0)
    return i


def _tail_tags_start(data):
    end = len(data)
    if end >= 128 and data[end - 128:end - 125] == b"TAG":
        end -= 128
    if end >= 32 and data[end - 32:end - 24] == b"APETAGEX":
        size = struct.unpack("<I", data[end - 20:end - 16])[0]
        end -= size + (32 if data[end - 32 + 23] & 0x80 else 0)
    return max(end, 0)


@dataclass(frozen=True)
class Mp3:
    frames: bytes  # the audio frames only, tags and info frame stripped
    version: int
    layer: int
    rate: int
    channels: int
    samples: int

    @property
    def duration(self):
        return self.samples / self.rate

    def joins(self, other):
        return (self.version, self.layer, self.rate, self.channels) == (
            other.version, other.layer, other.rate, other.channels)


def parse_mp3(data):
    """The frames of an MP3. Raises AudioError when there are none."""
    data = bytes(data)
    i, end = _id3v2_end(data), _tail_tags_start(data)
    out, first, samples, skipped = bytearray(), None, 0, 0
    while i < end:
        f = _frame(data, i)
        # A frame header must be followed by another (or the end) to count,
        # so stray 0xFFE bytes in junk aren't taken for audio.
        if f is None or i + f.length > end or (i + f.length < end - 3 and _frame(data, i + f.length) is None
                                                and first is None):
            i += 1
            skipped += 1
            if skipped > 64 * 1024:
                break
            continue
        if first is None:
            first = f
            if _is_info_frame(data, i, f):
                i += f.length
                continue
        if (f.version, f.layer, f.rate, f.channels) != (first.version, first.layer, first.rate, first.channels):
            i += 1  # a false sync inside a frame; real frames don't change format midway
            continue
        out += data[i:i + f.length]
        samples += f.samples
        i += f.length
    if first is None or not out:
        raise AudioError("not an MP3")
    return Mp3(bytes(out), first.version, first.layer, first.rate, first.channels, samples)


def wav_duration(data):
    """Seconds of PCM in a RIFF/WAVE file. Raises AudioError."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("not a WAV")
    i, byte_rate = 12, None
    while i + 8 <= len(data):
        cid, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if cid == b"fmt " and size >= 16:
            byte_rate = struct.unpack("<I", data[i + 16:i + 20])[0]
        elif cid == b"data":
            if not byte_rate:
                break
            return min(size, len(data) - i - 8) / byte_rate
        i += 8 + size + (size & 1)
    raise AudioError("WAV without fmt/data")


def sniff(data):
    """``"mp3"``, ``"wav"`` or None, from the first bytes."""
    head = bytes(data[:12])
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return WAV
    if head[:3] == b"ID3" or _frame(head, 0) is not None:
        return MP3
    return None


def duration(data, kind=None):
    """Seconds of audio in an MP3 or WAV. Raises AudioError."""
    kind = kind or sniff(data)
    if kind == MP3:
        return parse_mp3(data).duration
    if kind == WAV:
        return wav_duration(data)
    raise AudioError("not an MP3 or WAV")


def join(*mp3s):
    """One MP3 of ``mp3s`` back to back, or None when their formats don't match."""
    parsed = [parse_mp3(m) if not isinstance(m, Mp3) else m for m in mp3s]
    if any(not parsed[0].joins(p) for p in parsed[1:]):
        return None
    first = parsed[0]
    return Mp3(b"".join(p.frames for p in parsed), first.version, first.layer, first.rate, first.channels,
               sum(p.samples for p in parsed))
