"""Bounded, offline Story layouts. Full cover artwork is always preserved in front."""
from colorsys import rgb_to_hsv
from functools import lru_cache
from pathlib import Path
import unicodedata
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from app.core import HubError

STORY_PROFILE = 'cinematic-release-v2'
SIZE = (1080, 1920)
COVER_WIDTH = 840
# Coordinates describe reserved regions, not baked-in stickers or visible labels.
MUSIC_SAFE_BOX = (120, 1460, 960, 1620)
BOTTOM_SAFE_TOP = 1620
FONTS = Path(__file__).resolve().parent.parent / 'static' / 'fonts'


@lru_cache(maxsize=20)
def font(name, size):
    return ImageFont.truetype(str(FONTS / name), size=size)


def _mix(a, b, proportion):
    return tuple(round(x*(1-proportion)+y*proportion) for x,y in zip(a,b))


def _luminance(color):
    values = [v/255 for v in color]
    linear = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in values]
    return sum(v*w for v,w in zip(linear, (.2126,.7152,.0722)))


def cover_accent(image):
    """Use actual cover colors; favor warm light when present, otherwise its own hue."""
    sample = image.resize((80, 80), Image.Resampling.LANCZOS).quantize(colors=24).convert('RGB')
    candidates = []
    for count, color in sample.getcolors(6400):
        hue, saturation, brightness = rgb_to_hsv(*(v/255 for v in color))
        if saturation < .25 or not .25 < brightness < .98:
            continue
        warm = hue < .17 or hue > .94
        score = count ** .5 * saturation * brightness * (1.7 if warm else 1)
        candidates.append((score, color))
    selected = max(candidates)[1] if candidates else (205, 174, 121)
    # Luminous enough for the heading while retaining its source hue.
    accent = _mix(selected, (255, 235, 190), .48)
    # Keep cool/dark palettes readable too, without forcing them to become gold.
    while _luminance(accent) < .55:
        accent = _mix(accent, (255, 244, 214), .12)
    return accent


def _safe_announcement(text, face):
    text = ' '.join(unicodedata.normalize('NFC', text).split())
    missing = face.getmask('\uffff')
    marker = (missing.size, bytes(missing))
    for char in set(text):
        if unicodedata.category(char).startswith('C'):
            raise HubError('invalid_story_text', 'Use printable text; add unsupported symbols directly in Instagram')
        if not char.isspace():
            glyph = face.getmask(char)
            if (glyph.size, bytes(glyph)) == marker:
                raise HubError('invalid_story_text', 'This character is not supported by the Story font; use plain text and add emoji inside Instagram')
    return text


def announcement_lines(text):
    """Wrap by measured pixels, including long words, with a fixed safe text region."""
    face = font('DejaVuSans.ttf', 32)
    text = _safe_announcement(text, face)
    lines, line = [], ''
    for word in text.split():
        proposed = (line+' '+word).strip()
        if face.getlength(proposed) <= 880:
            line = proposed
            continue
        if line:
            lines.append(line)
            line = ''
        for char in word:
            if face.getlength(line+char) > 880:
                lines.append(line)
                line = ''
            line += char
    if line:lines.append(line)
    if len(lines) > 3:
        raise HubError('invalid_story_text', 'Shorten the announcement to fit three readable lines above the cover')
    return lines


def _text(canvas, text, top, face, fill, tracking=0):
    draw = ImageDraw.Draw(canvas)
    if not tracking:
        left, upper, right, lower = draw.textbbox((0,0), text, font=face)
        draw.text(((1080-(right-left))/2-left, top-upper), text, font=face, fill=fill)
        return (top, top+lower-upper)
    width = sum(face.getlength(c) for c in text) + tracking*(len(text)-1)
    upper = draw.textbbox((0,0), text, font=face)[1]
    x = (1080-width)/2
    for char in text:
        draw.text((x, top-upper), char, font=face, fill=fill)
        x += face.getlength(char)+tracking


def render_cinematic(image, announcement=''):
    """Render a single RGB frame; blur/lighting masks use a 360x640 working canvas."""
    lines = announcement_lines(announcement)
    accent = cover_accent(image)
    # Crop ONLY the enlarged background. The sharp foreground uses contain below.
    small = ImageOps.fit(image, (360,640), Image.Resampling.LANCZOS)
    small = small.filter(ImageFilter.GaussianBlur(24))
    small = Image.blend(small, Image.new('RGB', small.size, (10,24,49)), .20)
    # Gentle blue toning above; a darker, quiet region for the native Music sticker below.
    gradient = Image.new('L', (1,640))
    gradient.putdata([round(255*(.08+.40*max(0,(y-340)/300))) for y in range(640)])
    small = Image.composite(Image.new('RGB', small.size, (7,18,38)), small, gradient.resize(small.size))
    cover = ImageOps.contain(image, (COVER_WIDTH,COVER_WIDTH), Image.Resampling.LANCZOS)
    cover_top = 415+15*len(lines) if lines else 410
    x, y = (1080-cover.width)//2, cover_top+(840-cover.height)//2
    bounds = (x/3,y/3,(x+cover.width)/3,(y+cover.height)/3)
    glow = Image.new('L', small.size)
    ImageDraw.Draw(glow).rectangle(bounds, fill=45)
    glow = glow.filter(ImageFilter.GaussianBlur(25))
    small = Image.composite(Image.new('RGB', small.size, accent), small, glow)
    shadow = Image.new('L', small.size)
    ImageDraw.Draw(shadow).rectangle(tuple(v+(6 if i%2 else 0) for i,v in enumerate(bounds)), fill=160)
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    small = Image.composite(Image.new('RGB', small.size, (0,5,13)), small, shadow)
    canvas = small.resize(SIZE, Image.Resampling.LANCZOS)
    canvas.paste(cover, (x,y))
    _text(canvas, 'OUT NOW', 240 if lines else 260, font('DejaVuSerif-Bold.ttf',92 if lines else 100), accent, tracking=3)
    if lines:
        for index, line in enumerate(lines):
            _text(canvas, line, 345+index*34, font('DejaVuSans.ttf',32), (248,243,232))
    else:
        draw = ImageDraw.Draw(canvas)
        draw.line((405,370,516,370), fill=accent, width=2)
        draw.line((564,370,675,370), fill=accent, width=2)
        draw.polygon([(540,363),(547,370),(540,377),(533,370)], fill=accent)
    _text(canvas, 'PIXELPAPA', 1340, font('DejaVuSans-Bold.ttf',64), (249,244,233), tracking=8)
    _text(canvas, 'NEW SINGLE', 1420, font('DejaVuSans.ttf',26), accent, tracking=5)
    # No text, music-title facsimile, fake sticker or critical content after y=1460.
    return canvas
