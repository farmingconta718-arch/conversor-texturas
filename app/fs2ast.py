# -*- coding: utf-8 -*-
"""
fs2ast.py — Conversor DDS -> .ast (container GS2D do Farming Simulator 20 Android).

Reconstrucao fiel do pipeline validado no projeto BH DROID:
  - Le DDS (FourCC legado + DX10/dxgiFormat), incl. texture arrays.
  - Decodifica BC1/BC2/BC3 (Pillow) e BC4/BC5 (decoder proprio; Pillow nao le BC5).
  - Flip VERTICAL (o FS20 guarda as texturas invertidas de cima pra baixo).
  - Gera mipmaps, encoda ASTC 6x6 com astcenc, monta header GS2D de 60 bytes.
  - Auto-classifica pelo nome + arraySize (terreno / distance / ground /
    groundLayer / building array / detail / generico / specular).
  - detailArray -> formato fmt=5 (RGBA8 + zlib nivel 9), NAO ASTC.

Header GS2D (15 uint32 LE, 60 bytes):
  [0]=magic 'GS2D' [1]=4 [2]=payload [3]=w [4]=h [5]=1 [6]=canais
  [7]=mips-1 [8]=camadas [9]=2 [10]=fmt(24=ASTC6x6, 5=RGBA8+zlib)
  [11]=0 [12]=0(fmt5:1) [13]=0(fmt5:1) [14]=payload(fmt5: tam. cru)

Depende de Pillow (PIL) e numpy. astcenc: caminho passado em ASTCENC
(no APK = libastcenc.so extraido em nativeLibraryDir).
"""

import os
import struct
import subprocess
import tempfile
import zlib

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
try:
    from PIL import ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
except Exception:
    pass

# ---------------- configuracao ----------------
ASTCENC = os.environ.get("ASTCENC", "astcenc")   # caminho do binario
BLOCK = "6x6"
QUALITY = os.environ.get("ASTC_QUALITY", "-medium")
FLIPV = os.environ.get("FLIPV", "1") != "0"       # flip vertical (padrao ON)
SCALE = float(os.environ.get("SCALE", "50")) / 100.0   # 50% p/ building e genericos
SPEC = float(os.environ.get("SPEC", "50")) / 100.0     # specular = metade da escala

FMT_ASTC_6x6 = 24
FMT_RGBA8_ZLIB = 5

# materiais de terreno (classe "simples": 256, ch4, 7 mips)
TERRAIN_MATS = ("asphalt", "grass", "mud", "dirt", "roughmud", "rock",
                "sand", "gravel", "stone", "concrete", "forestground",
                "dirtgrassmix")

# ---------------- DDS parsing ----------------
DDPF_FOURCC = 0x4

DXGI_BC = {
    70: "BC1", 71: "BC1", 72: "BC1",
    73: "BC2", 74: "BC2", 75: "BC2",
    76: "BC3", 77: "BC3", 78: "BC3",
    79: "BC4", 80: "BC4", 81: "BC4",
    82: "BC5", 83: "BC5", 84: "BC5",
}
DXGI_SNORM = {81, 84}  # BC4S / BC5S


class DDS:
    def __init__(self, path):
        with open(path, "rb") as f:
            self.raw = f.read()
        self.path = path
        self._parse()

    def _parse(self):
        d = self.raw
        if d[:4] != b"DDS ":
            raise ValueError("nao e DDS: %s" % self.path)
        (size, flags, height, width, pitch, depth, mipcount) = struct.unpack_from("<7I", d, 4)
        # DDS_PIXELFORMAT: offset absoluto 76 (4 magic + 72 no header)
        pf_off = 76
        pf_size, pf_flags = struct.unpack_from("<II", d, pf_off)
        fourcc = struct.unpack_from("<4s", d, pf_off + 8)[0]
        rgb_bits, rmask, gmask, bmask, amask = struct.unpack_from("<5I", d, pf_off + 12)
        self.width = width
        self.height = height
        self.mipcount = mipcount if mipcount > 0 else 1
        self.array_size = 1
        self.snorm = False
        self.data_off = 4 + 124
        self.fmt = None

        if fourcc == b"DX10":
            dxgi, res_dim, misc, arr, misc2 = struct.unpack_from("<5I", d, 4 + 124)
            self.data_off = 4 + 124 + 20
            self.array_size = max(1, arr)
            self.fmt = DXGI_BC.get(dxgi)
            self.snorm = dxgi in DXGI_SNORM
            if self.fmt is None and dxgi == 28:
                self.fmt = "RGBA8"
        else:
            fc = fourcc
            if fc == b"DXT1":
                self.fmt = "BC1"
            elif fc == b"DXT3":
                self.fmt = "BC2"
            elif fc == b"DXT5":
                self.fmt = "BC3"
            elif fc in (b"ATI1", b"BC4U"):
                self.fmt = "BC4"
            elif fc in (b"BC4S",):
                self.fmt = "BC4"; self.snorm = True
            elif fc in (b"ATI2", b"BC5U"):
                self.fmt = "BC5"
            elif fc in (b"BC5S",):
                self.fmt = "BC5"; self.snorm = True
            elif not (pf_flags & DDPF_FOURCC):
                self.fmt = "RGBA8"
        if self.fmt is None:
            raise ValueError("formato DDS nao suportado (%r) em %s" % (fourcc, self.path))

    def block_bytes(self):
        return 8 if self.fmt in ("BC1", "BC4") else 16

    def has_alpha(self):
        return self.fmt in ("BC2", "BC3")

    # ---- extrai UMA camada como imagem RGBA (mip 0) ----
    def slice_image(self, layer=0):
        return _decode_slice(self, layer)


def _mip_sizes(w, h, mips, block):
    out = []
    for _ in range(mips):
        bw = max(1, (w + 3) // 4)
        bh = max(1, (h + 3) // 4)
        out.append((w, h, bw * bh * block))
        w = max(1, w // 2)
        h = max(1, h // 2)
    return out


def _layer_byte_size(dds):
    total = 0
    for (_, _, sz) in _mip_sizes(dds.width, dds.height, dds.mipcount, dds.block_bytes()):
        total += sz
    return total


def _decode_slice(dds, layer):
    """Decodifica o mip0 da camada `layer` para PIL RGBA."""
    fmt = dds.fmt
    layer_size = _layer_byte_size(dds)
    base = dds.data_off + layer * layer_size
    bw = max(1, (dds.width + 3) // 4)
    bh = max(1, (dds.height + 3) // 4)
    mip0_size = bw * bh * dds.block_bytes()
    block_data = dds.raw[base:base + mip0_size]

    if fmt in ("BC1", "BC2", "BC3"):
        # embrulha essa camada num DDS legado de 1 mip pro Pillow
        legacy = _wrap_legacy_dds(dds.width, dds.height, fmt, block_data)
        img = Image.open(_BytesIO(legacy))
        img.load()
        return img.convert("RGBA")
    elif fmt in ("BC4", "BC5"):
        arr = _decode_bc45(block_data, dds.width, dds.height, fmt, dds.snorm)
        return Image.fromarray(arr, "RGBA")
    elif fmt == "RGBA8":
        arr = np.frombuffer(dds.raw[base:base + dds.width * dds.height * 4], np.uint8)
        arr = arr.reshape(dds.height, dds.width, 4).copy()
        return Image.fromarray(arr, "RGBA")
    raise ValueError("decode nao suportado: %s" % fmt)


def _wrap_legacy_dds(w, h, fmt, block_data):
    fourcc = {"BC1": b"DXT1", "BC2": b"DXT3", "BC3": b"DXT5"}[fmt]
    header = bytearray(128)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)          # dwSize
    struct.pack_into("<I", header, 8, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)  # flags + linearsize
    struct.pack_into("<I", header, 12, h)
    struct.pack_into("<I", header, 16, w)
    bw = max(1, (w + 3) // 4); bh = max(1, (h + 3) // 4)
    block = 8 if fmt == "BC1" else 16
    struct.pack_into("<I", header, 20, bw * bh * block)  # pitchOrLinearSize
    struct.pack_into("<I", header, 28, 1)               # mipcount
    # pixelformat @ 76
    struct.pack_into("<I", header, 76, 32)             # pf size
    struct.pack_into("<I", header, 80, DDPF_FOURCC)    # pf flags
    header[84:88] = fourcc
    struct.pack_into("<I", header, 108, 0x1000)        # caps texture
    return bytes(header) + block_data


class _BytesIO:
    """io.BytesIO minimo para Pillow."""
    def __new__(cls, data):
        import io
        return io.BytesIO(data)


# ---- BC4/BC5 decode ----
def _decode_bc4_channel(data, w, h, snorm):
    bw = (w + 3) // 4
    bh = (h + 3) // 4
    out = np.zeros((bh * 4, bw * 4), np.uint8)
    off = 0
    for by in range(bh):
        for bx in range(bw):
            e0 = data[off]; e1 = data[off + 1]
            bits = int.from_bytes(data[off + 2:off + 8], "little")
            off += 8
            if snorm:
                r0 = _snorm8(e0); r1 = _snorm8(e1)
            else:
                r0 = e0; r1 = e1
            palette = _bc4_palette(r0, r1)
            for py in range(4):
                for px in range(4):
                    idx = (bits >> (3 * (py * 4 + px))) & 0x7
                    out[by * 4 + py, bx * 4 + px] = palette[idx]
    return out[:h, :w]


def _snorm8(v):
    s = v if v < 128 else v - 256   # int8
    s = max(s, -127)
    return int(round((s + 127) * 255.0 / 254.0))


def _bc4_palette(r0, r1):
    p = [0] * 8
    p[0] = r0; p[1] = r1
    if r0 > r1:
        for i in range(1, 7):
            p[i + 1] = ((7 - i) * r0 + i * r1) // 7
    else:
        for i in range(1, 5):
            p[i + 1] = ((5 - i) * r0 + i * r1) // 5
        p[6] = 0; p[7] = 255
    return p


def _decode_bc45(data, w, h, fmt, snorm):
    rgba = np.zeros((h, w, 4), np.uint8)
    if fmt == "BC4":
        r = _decode_bc4_channel(data, w, h, snorm)
        rgba[..., 0] = r; rgba[..., 1] = r; rgba[..., 2] = r; rgba[..., 3] = 255
        return rgba
    # BC5 = 2 blocos BC4 (R e G) intercalados a cada 16 bytes
    red = _decode_bc4_channel(_deinterleave(data, 0), w, h, snorm)
    grn = _decode_bc4_channel(_deinterleave(data, 1), w, h, snorm)
    rgba[..., 0] = red
    rgba[..., 1] = grn
    # reconstroi Z (normal map): B = sqrt(1 - x^2 - y^2)
    x = red.astype(np.float32) / 127.5 - 1.0
    y = grn.astype(np.float32) / 127.5 - 1.0
    z = np.sqrt(np.clip(1.0 - x * x - y * y, 0.0, 1.0))
    rgba[..., 2] = np.clip((z + 1.0) * 127.5, 0, 255).astype(np.uint8)
    rgba[..., 3] = 255
    return rgba


def _deinterleave(data, which):
    """Pega os blocos BC4 pares (which=0) ou impares (which=1) de um stream BC5."""
    out = bytearray()
    n = len(data) // 16
    for i in range(n):
        blk = data[i * 16 + which * 8: i * 16 + which * 8 + 8]
        out += blk
    return bytes(out)


# ---------------- ASTC ----------------
def _run_astcenc(img_rgba, block=BLOCK, quality=QUALITY):
    """Encoda uma imagem PIL RGBA em blocos ASTC crus (sem o header .astc)."""
    with tempfile.TemporaryDirectory() as td:
        inp = os.path.join(td, "in.png")
        out = os.path.join(td, "out.astc")
        img_rgba.save(inp)
        mode = os.environ.get("ASTC_MODE", "-cl")   # -cl LDR linear ; -cs sRGB
        cmd = [ASTCENC, mode, inp, out, block, quality, "-silent"]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        with open(out, "rb") as f:
            blob = f.read()
        return blob[16:]   # tira o header de 16 bytes do .astc


def _build_mips(img, max_res=None, min_dim=4, max_mips=None):
    """Gera lista de imagens (mip0..mipN) reduzindo ate min_dim."""
    w, h = img.size
    if max_res and max(w, h) > max_res:
        r = max_res / float(max(w, h))
        w = max(1, int(round(w * r)))
        h = max(1, int(round(h * r)))
        img = img.resize((w, h), Image.LANCZOS)
    mips = [img]
    while min(img.size) > min_dim:
        w = max(1, img.size[0] // 2)
        h = max(1, img.size[1] // 2)
        img = img.resize((w, h), Image.LANCZOS)
        mips.append(img)
        if max_mips and len(mips) >= max_mips:
            break
    return mips


def gs2d_header(w, h, channels, mips, layers, fmt, size2, size14,
                idx9=2, idx11=0, idx12=0, idx13=0):
    return b"GS2D" + struct.pack("<14I", 4, size2, w, h, 1, channels,
                                 mips - 1, layers, idx9, fmt,
                                 idx11, idx12, idx13, size14)


# ---------------- classificacao ----------------
def classify(path, array_size=1):
    name = os.path.basename(path).lower()
    stem = os.path.splitext(name)[0]
    if "detailarray" in stem or (array_size >= 20 and "detail" in stem):
        return "detail"
    if "_distance" in stem:
        return "distance"
    if array_size > 1:
        if "ground" in stem or "groundlayer" in stem:
            return "ground_array"
        return "building_array"
    if any(m in stem for m in TERRAIN_MATS) and "foliage" not in stem:
        return "terrain_simple"
    return "specular" if "_specular" in stem or "_spec" in stem else "generic"


def _ast_params(kind, channels):
    """Resolucao maxima e escala por classe de textura."""
    max_res = None
    scale = 1.0
    if kind == "terrain_simple":
        max_res = 256
    elif kind == "distance":
        max_res = 128; channels = 3
    elif kind == "ground_array":
        max_res = 512
    elif kind == "building_array":
        scale = SCALE
    elif kind == "generic":
        scale = SCALE
    elif kind == "specular":
        scale = SCALE * SPEC
    return channels, max_res, scale


def _encode_ast(get_layer, layers, channels, kind, out_path, name, log):
    """Pipeline comum: flip -> escala -> mips -> astcenc 6x6 -> header GS2D."""
    channels, max_res, scale = _ast_params(kind, channels)
    payload = bytearray()
    total_mips = 0
    out_w = out_h = 0
    for layer in range(layers):
        img = get_layer(layer)
        if FLIPV:
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        if scale != 1.0:
            w = max(1, int(round(img.size[0] * scale)))
            h = max(1, int(round(img.size[1] * scale)))
            img = img.resize((w, h), Image.LANCZOS)
        mips = _build_mips(img, max_res=max_res)
        if layer == 0:
            out_w, out_h = mips[0].size
            total_mips = len(mips)
        for m in mips:
            payload += _run_astcenc(m)

    size = len(payload)
    header = gs2d_header(out_w, out_h, channels, total_mips, layers,
                         FMT_ASTC_6x6, size, size)
    with open(out_path, "wb") as f:
        f.write(header)
        f.write(payload)
    log("  %-14s %dx%d ch%d %dmip x%dcam -> %s (%d bytes)" %
        (kind, out_w, out_h, channels, total_mips, layers,
         os.path.basename(out_path), size + 60))
    return out_path


# ---------------- conversao principal ----------------
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".tga", ".webp", ".gif")


def convert_file(path, out_path=None, log=print):
    """Converte DDS, JPG, PNG (etc.) em .ast. Retorna o caminho de saida."""
    ext = os.path.splitext(path.lower())[1]
    if ext == ".dds":
        return convert_dds(path, out_path, log)
    if ext in IMAGE_EXTS:
        return convert_image(path, out_path, log)
    raise ValueError("formato de entrada nao suportado: %s" % ext)


def convert_image(path, out_path=None, log=print):
    """Converte PNG/JPG/etc. em .ast (via Pillow -> mesmo pipeline ASTC)."""
    if out_path is None:
        out_path = os.path.splitext(path)[0] + ".ast"
    img = Image.open(path)
    img.load()
    has_alpha = "A" in img.getbands() or img.mode in ("RGBA", "LA", "PA")
    img = img.convert("RGBA")
    channels = 4 if has_alpha else 3
    kind = classify(path, array_size=1)
    if kind == "detail":   # sem array numa imagem solta -> trata como generico
        kind = "generic"
    return _encode_ast(lambda _i: img, 1, channels, kind, out_path,
                       os.path.basename(path), log)


def convert_dds(path, out_path=None, log=print):
    """Converte um .dds em .ast. Retorna o caminho de saida."""
    dds = DDS(path)
    kind = classify(path, array_size=dds.array_size)
    if out_path is None:
        out_path = os.path.splitext(path)[0] + ".ast"

    if kind == "detail":
        _convert_detail(dds, out_path)
        log("  detail -> %s" % os.path.basename(out_path))
        return out_path

    channels = 4 if dds.has_alpha() or dds.fmt in ("BC5", "RGBA8") else 3
    if dds.fmt == "BC1":
        channels = 3
    return _encode_ast(dds.slice_image, dds.array_size, channels, kind,
                       out_path, os.path.basename(path), log)


def _convert_detail(dds, out_path):
    """detailArray -> fmt=5: tira Nx1 (cor media por camada), zlib nivel 9."""
    n = dds.array_size
    layers_out = max(1, n - 1)   # o jogo descarta a ultima camada
    rgba = bytearray()
    for layer in range(layers_out):
        img = dds.slice_image(layer).convert("RGBA")
        one = img.resize((1, 1), Image.BOX)
        r, g, b, _ = one.getpixel((0, 0))
        rgba += bytes((r, g, b, 255))
    raw = bytes(rgba)
    comp = zlib.compress(raw, 9)
    header = gs2d_header(layers_out, 1, 4, 1, 1, FMT_RGBA8_ZLIB,
                         len(comp), len(raw), idx9=2, idx12=1, idx13=1)
    with open(out_path, "wb") as f:
        f.write(header)
        f.write(comp)
    return out_path


def convert_folder(folder, log=print,
                   only_exts=(".dds", ".png", ".jpg", ".jpeg"),
                   recursive=True, delete_src=False):
    """Converte DDS/JPG/PNG de uma pasta para .ast (ao lado do original)."""
    done = 0
    fail = 0
    walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
    for dirpath, _dirs, files in walker:
        for fn in files:
            if os.path.splitext(fn.lower())[1] in only_exts:
                src = os.path.join(dirpath, fn)
                try:
                    convert_file(src, log=log)
                    if delete_src:
                        os.remove(src)
                    done += 1
                except Exception as e:
                    fail += 1
                    log("  ! FALHOU %s: %s" % (fn, e))
    log("Conversao: %d ok, %d falhas" % (done, fail))
    return done, fail


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("uso: fs2ast.py <pasta_ou_arquivo> (.dds/.jpg/.png)")
        sys.exit(1)
    tgt = sys.argv[1]
    if os.path.isdir(tgt):
        convert_folder(tgt)
    else:
        convert_file(tgt)
