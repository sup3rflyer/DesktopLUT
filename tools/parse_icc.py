import struct
import sys
import os
import math

def read_s15f16(data, off):
    raw = struct.unpack_from('>i', data, off)[0]
    return raw / 65536.0

def read_u16f16(data, off):
    raw = struct.unpack_from('>I', data, off)[0]
    return raw / 65536.0

def read_u8f8(data, off):
    raw = struct.unpack_from('>H', data, off)[0]
    return raw / 256.0

def pq_eotf(pq):
    """ST.2084 PQ EOTF: PQ signal (0-1) -> linear nits (0-10000)"""
    m1 = 0.1593017578125
    m2 = 78.84375
    c1 = 0.8359375
    c2 = 18.8515625
    c3 = 18.6875
    if pq <= 0:
        return 0.0
    Np = pq ** (1.0 / m2)
    num = max(Np - c1, 0.0)
    den = c2 - c3 * Np
    if den <= 0:
        return 0.0
    return 10000.0 * ((num / den) ** (1.0 / m1))

def pq_oetf(nits):
    """ST.2084 PQ OETF: linear nits (0-10000) -> PQ signal (0-1)"""
    m1 = 0.1593017578125
    m2 = 78.84375
    c1 = 0.8359375
    c2 = 18.8515625
    c3 = 18.6875
    Y = max(nits / 10000.0, 0.0)
    Ym1 = Y ** m1
    return ((c1 + c2 * Ym1) / (1.0 + c3 * Ym1)) ** m2

def parse_header(data):
    print("=" * 70)
    print("ICC HEADER")
    print("=" * 70)
    size = struct.unpack_from('>I', data, 0)[0]
    print(f"  Profile size: {size} bytes")
    cmm = data[4:8].decode('ascii', errors='replace')
    print(f"  CMM type: '{cmm}'")
    ver_major = data[8]
    ver_minor = (data[9] >> 4) & 0xF
    ver_bugfix = data[9] & 0xF
    print(f"  Version: {ver_major}.{ver_minor}.{ver_bugfix}")
    devclass = data[12:16].decode('ascii', errors='replace')
    print(f"  Device class: '{devclass}'")
    colorspace = data[16:20].decode('ascii', errors='replace')
    print(f"  Color space: '{colorspace}'")
    pcs = data[20:24].decode('ascii', errors='replace')
    print(f"  PCS: '{pcs}'")
    yr = struct.unpack_from('>H', data, 24)[0]
    mo = struct.unpack_from('>H', data, 26)[0]
    dy = struct.unpack_from('>H', data, 28)[0]
    hr = struct.unpack_from('>H', data, 30)[0]
    mn = struct.unpack_from('>H', data, 32)[0]
    sc = struct.unpack_from('>H', data, 34)[0]
    print(f"  Date/Time: {yr}-{mo:02d}-{dy:02d} {hr:02d}:{mn:02d}:{sc:02d}")
    sig = data[36:40].decode('ascii', errors='replace')
    print(f"  File signature: '{sig}'")
    platform = data[40:44].decode('ascii', errors='replace')
    print(f"  Platform: '{platform}'")
    flags = struct.unpack_from('>I', data, 44)[0]
    print(f"  Profile flags: 0x{flags:08x}")
    manufacturer = data[48:52]
    print(f"  Device manufacturer: {manufacturer.hex()}")
    model = data[52:56]
    print(f"  Device model: {model.hex()}")
    attrs = struct.unpack_from('>Q', data, 56)[0]
    print(f"  Device attributes: 0x{attrs:016x}")
    intent = struct.unpack_from('>I', data, 64)[0]
    intents = {0: 'Perceptual', 1: 'Relative Colorimetric', 2: 'Saturation', 3: 'Absolute Colorimetric'}
    print(f"  Rendering intent: {intent} ({intents.get(intent, 'Unknown')})")
    ix = read_s15f16(data, 68)
    iy = read_s15f16(data, 72)
    iz = read_s15f16(data, 76)
    print(f"  PCS illuminant XYZ: ({ix:.6f}, {iy:.6f}, {iz:.6f})")
    creator = data[80:84].decode('ascii', errors='replace')
    print(f"  Profile creator: '{creator}'")
    profile_id = data[84:100].hex()
    print(f"  Profile ID (MD5): {profile_id}")
    return size

def parse_tags(data):
    print("\n" + "=" * 70)
    print("TAG TABLE")
    print("=" * 70)
    tag_count = struct.unpack_from('>I', data, 128)[0]
    print(f"  Tag count: {tag_count}")
    tags = {}
    for i in range(tag_count):
        off = 132 + i * 12
        sig = data[off:off+4].decode('ascii', errors='replace')
        tag_off = struct.unpack_from('>I', data, off+4)[0]
        tag_size = struct.unpack_from('>I', data, off+8)[0]
        print(f"  [{i:2d}] '{sig}' offset={tag_off} size={tag_size}")
        tags[sig] = (tag_off, tag_size)
    return tags

def parse_xyz_tag(data, off, size, name):
    typ = data[off:off+4].decode('ascii', errors='replace')
    x = read_s15f16(data, off+8)
    y = read_s15f16(data, off+12)
    z = read_s15f16(data, off+16)
    # Convert to CIE xy
    total = x + y + z
    if total > 0:
        cx = x / total
        cy = y / total
        print(f"  {name}: X={x:.6f} Y={y:.6f} Z={z:.6f}  -> CIE xy=({cx:.4f}, {cy:.4f})")
    else:
        print(f"  {name}: X={x:.6f} Y={y:.6f} Z={z:.6f}")
    return (x, y, z)

def parse_trc(data, off, size, name):
    typ = data[off:off+4].decode('ascii', errors='replace')
    if typ == 'curv':
        count = struct.unpack_from('>I', data, off+8)[0]
        if count == 0:
            print(f"  {name}: identity (type='curv', count=0)")
            return []
        elif count == 1:
            gamma = read_u8f8(data, off+12)
            print(f"  {name}: gamma={gamma:.4f} (type='curv', count=1)")
            return [gamma]
        else:
            vals = []
            for i in range(count):
                v = struct.unpack_from('>H', data, off+12+i*2)[0] / 65535.0
                vals.append(v)
            mid = count // 2
            q1 = count // 4
            q3 = 3 * count // 4
            print(f"  {name}: tabular, {count} entries (type='curv')")
            print(f"    [0]={vals[0]:.6f} [25%]={vals[q1]:.6f} [50%]={vals[mid]:.6f} [75%]={vals[q3]:.6f} [{count-1}]={vals[-1]:.6f}")
            max_dev = 0
            max_dev_idx = 0
            for i in range(count):
                expected = i / (count - 1)
                dev = abs(vals[i] - expected)
                if dev > max_dev:
                    max_dev = dev
                    max_dev_idx = i
            print(f"    Max deviation from identity: {max_dev:.6f} at index {max_dev_idx} (input={max_dev_idx/(count-1):.4f})")
            # Try to fit gamma
            sum_log = 0
            n_valid = 0
            for i in range(1, count-1):
                x = i / (count - 1)
                y = vals[i]
                if y > 0.01 and x > 0.01:
                    sum_log += math.log(y) / math.log(x)
                    n_valid += 1
            if n_valid > 0:
                avg_gamma = sum_log / n_valid
                print(f"    Approximate effective gamma: {avg_gamma:.4f}")
            # Check if TRC looks like PQ response (normalized to some peak)
            # Test at several points: compute ideal PQ normalized to estimated peak
            # Try to find the peak nits that best fits the TRC as a PQ response
            best_peak = 0
            best_err = 1e9
            for test_peak in range(200, 3000, 10):
                err = 0
                n = 0
                for i in [count//8, count//4, count//2, 3*count//4]:
                    sig = i / (count - 1)
                    ideal = pq_eotf(sig) / test_peak  # pq_eotf returns nits (0-10000)
                    if ideal <= 1.0:
                        err += (vals[i] - ideal) ** 2
                        n += 1
                if n > 0 and err / n < best_err:
                    best_err = err / n
                    best_peak = test_peak
            if best_peak > 0:
                rmse = math.sqrt(best_err)
                print(f"    Best PQ fit: peak={best_peak} nits (RMSE={rmse:.6f})")
                if rmse < 0.05:
                    print(f"    ** TRC IS PQ-LIKE (profiled in HDR mode)")
                    # Show deviations from ideal PQ at key points
                    for pct_idx in [count//8, count//4, 3*count//8, count//2, 5*count//8, 3*count//4, 7*count//8]:
                        sig = pct_idx / (count - 1)
                        ideal_nits = pq_eotf(sig) * 10000.0
                        ideal_norm = ideal_nits / best_peak
                        meas_norm = vals[pct_idx]
                        meas_nits = meas_norm * best_peak
                        pct_err = ((meas_nits - ideal_nits) / ideal_nits * 100) if ideal_nits > 0.01 else 0
                        print(f"      PQ {sig*100:5.1f}%: ideal={ideal_nits:7.1f} nits, measured~={meas_nits:7.1f} nits ({pct_err:+.1f}%)")
                elif rmse < 0.15:
                    print(f"    ** TRC is ROUGHLY PQ-like (possible HDR measurement with tracking errors)")
                else:
                    print(f"    ** TRC is NOT PQ-like (likely SDR gamma response)")
            return vals
    elif typ == 'para':
        func_type = struct.unpack_from('>H', data, off+8)[0]
        if func_type == 0:
            g = read_s15f16(data, off+12)
            print(f"  {name}: parametric type 0, gamma={g:.4f}")
        elif func_type == 3:
            g = read_s15f16(data, off+12)
            a = read_s15f16(data, off+16)
            b = read_s15f16(data, off+20)
            c = read_s15f16(data, off+24)
            d = read_s15f16(data, off+28)
            e = read_s15f16(data, off+32)
            f_val = read_s15f16(data, off+36)
            print(f"  {name}: parametric type 3 (sRGB-like)")
            print(f"    g={g:.4f} a={a:.4f} b={b:.4f} c={c:.4f} d={d:.4f} e={e:.4f} f={f_val:.4f}")
        else:
            print(f"  {name}: parametric type {func_type}")
        return []
    elif typ == 'sf32':
        count = (size - 8) // 4
        vals = []
        for i in range(count):
            v = read_s15f16(data, off+8+i*4)
            vals.append(v)
        print(f"  {name}: sf32 array, {count} entries")
        if count > 0:
            mid = count // 2
            q1 = count // 4
            q3 = 3 * count // 4
            print(f"    [0]={vals[0]:.6f} [25%]={vals[q1]:.6f} [50%]={vals[mid]:.6f} [75%]={vals[q3]:.6f} [{count-1}]={vals[-1]:.6f}")
            max_dev = 0
            max_dev_idx = 0
            for i in range(count):
                expected = i / (count - 1)
                dev = abs(vals[i] - expected)
                if dev > max_dev:
                    max_dev = dev
                    max_dev_idx = i
            print(f"    Max deviation from identity: {max_dev:.6f} at index {max_dev_idx} (input={max_dev_idx/(count-1):.4f})")
        return vals
    else:
        print(f"  {name}: unknown type '{typ}', size={size}")
        return []

def parse_chad(data, off, size):
    typ = data[off:off+4].decode('ascii', errors='replace')
    print(f"  chad (chromatic adaptation): type='{typ}'")
    m = []
    for i in range(9):
        m.append(read_s15f16(data, off+8+i*4))
    print(f"    [{m[0]:.6f} {m[1]:.6f} {m[2]:.6f}]")
    print(f"    [{m[3]:.6f} {m[4]:.6f} {m[5]:.6f}]")
    print(f"    [{m[6]:.6f} {m[7]:.6f} {m[8]:.6f}]")
    return m

def parse_lumi(data, off, size):
    typ = data[off:off+4].decode('ascii', errors='replace')
    x = read_s15f16(data, off+8)
    y = read_s15f16(data, off+12)
    z = read_s15f16(data, off+16)
    print(f"  lumi: X={x:.4f} Y={y:.4f} Z={z:.4f} (peak = {y:.1f} cd/m2)")
    return y

def parse_mhc2(data, off, size):
    print(f"\n  MHC2 TAG (offset={off}, size={size}):")
    typ = data[off:off+4].decode('ascii', errors='replace')
    print(f"    Type signature: '{typ}'")
    if size < 36:
        print(f"    ERROR: MHC2 tag too small ({size} bytes)")
        return
    lut_size = struct.unpack_from('>I', data, off+8)[0]
    min_cll = read_s15f16(data, off+12)
    max_cll = read_s15f16(data, off+16)
    print(f"    LUT size: {lut_size}")
    print(f"    MinCLL: {min_cll:.4f} nits")
    print(f"    MaxCLL: {max_cll:.4f} nits")

    # Header layout (36 bytes = 9 x uint32):
    #   +0:  'MHC2' signature
    #   +4:  reserved
    #   +8:  lutSize
    #   +12: MinCLL (s15f16)
    #   +16: MaxCLL (s15f16)
    #   +20: matrix offset (from tag start)
    #   +24: LUT 0 (Red) offset
    #   +28: LUT 1 (Green) offset
    #   +32: LUT 2 (Blue) offset
    off_mat = struct.unpack_from('>I', data, off+20)[0]
    off_r = struct.unpack_from('>I', data, off+24)[0]
    off_g = struct.unpack_from('>I', data, off+28)[0]
    off_b = struct.unpack_from('>I', data, off+32)[0]
    print(f"    Offsets (relative): matrix={off_mat} R={off_r} G={off_g} B={off_b}")

    # Matrix at off_mat from tag start
    mat_off = off + off_mat
    print(f"    Matrix (3x4, XYZ-to-XYZ):")
    matrix = []
    for r in range(3):
        row = []
        for c in range(4):
            v = read_s15f16(data, mat_off + (r*4+c)*4)
            row.append(v)
        matrix.append(row)
        print(f"      [{row[0]:+.6f} {row[1]:+.6f} {row[2]:+.6f} {row[3]:+.6f}]")

    is_identity = True
    for r in range(3):
        for c in range(4):
            expected = 1.0 if r == c else 0.0
            if abs(matrix[r][c] - expected) > 0.001:
                is_identity = False
    if is_identity:
        print(f"      ** Matrix is IDENTITY (no gamut correction)")

    # Parse each LUT channel (using correct offsets)
    peak_nits = max_cll if max_cll > 0 else 1000
    for ch_name, ch_off_rel in [('Red', off_r), ('Green', off_g), ('Blue', off_b)]:
        lut_abs_off = off + ch_off_rel
        if lut_abs_off + 8 + lut_size * 4 > len(data):
            print(f"    {ch_name} LUT: OFFSET OUT OF BOUNDS (off={ch_off_rel}, abs={lut_abs_off})")
            continue
        lut_sig = data[lut_abs_off:lut_abs_off+4].decode('ascii', errors='replace')
        vals = []
        for i in range(lut_size):
            v = read_s15f16(data, lut_abs_off + 8 + i*4)
            vals.append(v)

        if len(vals) > 0:
            mid = len(vals) // 2
            q1 = len(vals) // 4
            q3 = 3 * len(vals) // 4
            print(f"    {ch_name} LUT ({len(vals)} entries, type='{lut_sig}'):")
            print(f"      [0]={vals[0]:.6f} [25%]={vals[q1]:.6f} [50%]={vals[mid]:.6f} [75%]={vals[q3]:.6f} [{len(vals)-1}]={vals[-1]:.6f}")

            max_dev = 0
            max_dev_idx = 0
            for i in range(len(vals)):
                expected = i / (len(vals) - 1)
                dev = abs(vals[i] - expected)
                if dev > max_dev:
                    max_dev = dev
                    max_dev_idx = i
            print(f"      Max deviation from identity: {max_dev:.6f} at index {max_dev_idx} (input={max_dev_idx/(len(vals)-1):.4f})")

            if max_dev < 0.0002:
                print(f"      ** LUT IS IDENTITY (no correction applied!)")
            else:
                print(f"      Correction samples (PQ domain, peak={peak_nits:.0f} nits):")
                for pct in [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95]:
                    idx = int(pct * (len(vals) - 1))
                    expected = idx / (len(vals) - 1)
                    actual = vals[idx]
                    nits_in = pq_eotf(expected)
                    nits_out = pq_eotf(actual)
                    diff_pq = (actual - expected) * 100
                    print(f"        PQ {pct*100:5.1f}% ({nits_in:7.1f} nits): out={actual:.6f} ({nits_out:7.1f} nits) diff={diff_pq:+.4f}% PQ")

def parse_vcgt(data, off, size):
    typ = data[off:off+4].decode('ascii', errors='replace')
    print(f"  vcgt: type='{typ}', size={size}")
    if size > 12:
        tag_type = struct.unpack_from('>I', data, off+8)[0]
        if tag_type == 0:
            channels = struct.unpack_from('>H', data, off+12)[0]
            count = struct.unpack_from('>H', data, off+14)[0]
            bits = struct.unpack_from('>H', data, off+16)[0]
            print(f"    Table: {channels} channels, {count} entries, {bits}-bit")
            if count > 0 and bits == 16:
                for ch in range(min(channels, 3)):
                    ch_name = ['R', 'G', 'B'][ch]
                    max_dev = 0
                    max_dev_idx = 0
                    for i in range(count):
                        v = struct.unpack_from('>H', data, off+18+ch*count*2+i*2)[0] / 65535.0
                        expected = i / (count - 1)
                        dev = abs(v - expected)
                        if dev > max_dev:
                            max_dev = dev
                            max_dev_idx = i
                    if max_dev < 0.001:
                        print(f"    {ch_name}: IDENTITY")
                    else:
                        print(f"    {ch_name}: max deviation={max_dev:.6f} at idx {max_dev_idx}")

def parse_desc(data, off, size):
    typ = data[off:off+4].decode('ascii', errors='replace')
    if typ == 'mluc':
        count = struct.unpack_from('>I', data, off+8)[0]
        if count > 0:
            str_len = struct.unpack_from('>I', data, off+20)[0]
            str_off = struct.unpack_from('>I', data, off+24)[0]
            try:
                desc_str = data[off+str_off:off+str_off+str_len].decode('utf-16-be', errors='replace')
            except:
                desc_str = "<decode error>"
            print(f"  desc: '{desc_str}'")
    elif typ == 'desc':
        str_len = struct.unpack_from('>I', data, off+8)[0]
        desc_str = data[off+12:off+12+str_len-1].decode('ascii', errors='replace')
        print(f"  desc: '{desc_str}'")
    else:
        print(f"  desc: type='{typ}', size={size}")

def parse_profile(filepath):
    print(f"\n{'#' * 70}")
    print(f"# {os.path.basename(filepath)}")
    print(f"# Size: {os.path.getsize(filepath)} bytes")
    print(f"{'#' * 70}")

    with open(filepath, 'rb') as f:
        data = f.read()

    parse_header(data)
    tags = parse_tags(data)

    print("\n" + "=" * 70)
    print("TAG DETAILS")
    print("=" * 70)

    if 'desc' in tags:
        parse_desc(data, *tags['desc'])

    if 'cprt' in tags:
        off, sz = tags['cprt']
        typ = data[off:off+4].decode('ascii', errors='replace')
        if typ == 'mluc':
            count = struct.unpack_from('>I', data, off+8)[0]
            if count > 0:
                str_len = struct.unpack_from('>I', data, off+20)[0]
                str_off = struct.unpack_from('>I', data, off+24)[0]
                try:
                    s = data[off+str_off:off+str_off+str_len].decode('utf-16-be', errors='replace')
                except:
                    s = "?"
                print(f"  cprt: '{s}'")
        elif typ == 'text':
            s = data[off+8:off+sz].decode('ascii', errors='replace').rstrip('\x00')
            print(f"  cprt: '{s}'")

    if 'dmdd' in tags:
        off, sz = tags['dmdd']
        typ = data[off:off+4].decode('ascii', errors='replace')
        if typ == 'mluc':
            count = struct.unpack_from('>I', data, off+8)[0]
            if count > 0:
                str_len = struct.unpack_from('>I', data, off+20)[0]
                str_off = struct.unpack_from('>I', data, off+24)[0]
                try:
                    s = data[off+str_off:off+str_off+str_len].decode('utf-16-be', errors='replace')
                except:
                    s = "?"
                print(f"  dmdd: '{s}'")

    if 'wtpt' in tags:
        parse_xyz_tag(data, *tags['wtpt'], 'wtpt (white point)')
    if 'bkpt' in tags:
        parse_xyz_tag(data, *tags['bkpt'], 'bkpt (black point)')
    if 'lumi' in tags:
        parse_lumi(data, *tags['lumi'])

    if 'chrm' in tags:
        off, sz = tags['chrm']
        n_ch = struct.unpack_from('>H', data, off+8)[0]
        phos = struct.unpack_from('>H', data, off+10)[0]
        print(f"  chrm: {n_ch} channels, phosphor type={phos}")
        for i in range(min(n_ch, 3)):
            cx = read_u16f16(data, off+12+i*8)
            cy = read_u16f16(data, off+12+i*8+4)
            print(f"    {'RGB'[i]}: x={cx:.6f} y={cy:.6f}")

    for t in ['rXYZ', 'gXYZ', 'bXYZ']:
        if t in tags:
            parse_xyz_tag(data, *tags[t], t)

    if 'chad' in tags:
        parse_chad(data, *tags['chad'])

    print()
    for t in ['rTRC', 'gTRC', 'bTRC']:
        if t in tags:
            parse_trc(data, *tags[t], t)

    if 'vcgt' in tags:
        print()
        parse_vcgt(data, *tags['vcgt'])

    if 'MHC2' in tags:
        print()
        parse_mhc2(data, *tags['MHC2'])

    # Summary of tags NOT parsed
    known = {'desc','cprt','dmdd','wtpt','bkpt','lumi','chrm','rXYZ','gXYZ','bXYZ',
             'chad','rTRC','gTRC','bTRC','vcgt','MHC2'}
    unknown = set(tags.keys()) - known
    if unknown:
        print(f"\n  Other tags (not fully parsed): {', '.join(sorted(unknown))}")

    print()

if len(sys.argv) < 3:
    print("Usage: parse_icc.py <file1.icm> <file2.icm>")
    sys.exit(1)

parse_profile(sys.argv[1])
parse_profile(sys.argv[2])
