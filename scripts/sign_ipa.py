#!/usr/bin/env python3
# Sign an IPA that build_ipa_from_nsp.py built (any single-app Payload/*.app works): embed a provisioning profile and the entitlements derived from it,
# sign the app with a keychain identity, and repack the archive. The game files inside the app are GBs and codesign never touches them, so the repack copies
# every unchanged entry's compressed bytes straight from the original IPA; only the executable, Info.plist, _CodeSignature and embedded.mobileprovision are new.
# The app needs no entitlements to run its game code (the rewritten modules are remapped from its own __TEXT, loader.c); the bigger games also want
# Increased Memory Limit (--memory-limit, or it comes with the profile when the account provisions it).
# Usage: sign_ipa.py <in.ipa> [--out <signed.ipa>] [options]
#   --profile <file | auto>   the .mobileprovision to embed (auto: the newest valid profile in Xcode's profile folders that covers the bundle ID; a wildcard
#                            profile covers every game). Without one, let Xcode manage signing once for an app of that ID (or --prepare-xcode), or use a
#                            profile exported by a sideloading tool
#   --identity <name | auto | ->  the codesign identity (auto: the profile's certificate found in the keychain; '-' = ad-hoc, for TrollStore, no profile needed)
#   --bundle-id <id>          rewrite CFBundleIdentifier to fit a profile made for another ID (another ID is another app on the device, with its own saves)
#   --memory-limit            add com.apple.developer.kernel.increased-memory-limit: iOS gives about 3.4 GB without it and closes the bigger games. An
#                            entitlement the profile does not list may make a normal install refuse the app (TrollStore does not care)
#   --entitlements <plist>    merge these keys on top of the generated ones (a per-app override, or extra keys for TrollStore)
#   --no-file-sharing         leave Info.plist as built. By default signing turns file sharing on (UIFileSharingEnabled,
#                            LSSupportsOpeningDocumentsInPlace): the app's Documents — log.txt, saves, toggles.txt, env.txt —
#                            appear under "On My iPhone" in the Files app, and this repo's IPAs already carry the keys
#   --work <dir>              where to extract the app (default: a temp folder beside the IPA; its free space must hold the app's uncompressed size)
#   --keep-work               keep the extraction folder (debugging; it is deleted by default)
# A certificate that is not in the keychain yet (a .p12 from elsewhere) is imported first with: security import <cert.p12>
import argparse, concurrent.futures as cf, hashlib, os, plistlib, re, shutil, stat, struct, subprocess, sys, tempfile, time, zipfile, zlib
from datetime import datetime
from pathlib import Path

L32, L16 = 0xFFFFFFFF, 0xFFFF
CHUNK = 4 << 20
PROFILE_DIRS = ('~/Library/Developer/Xcode/UserData/Provisioning Profiles', '~/Library/MobileDevice/Provisioning Profiles')


def die(msg): sys.exit(f'[RED] sign_ipa: {msg}')


def run(cmd, what):
    r = subprocess.run([str(c) for c in cmd], capture_output=True)
    if r.returncode: die(f'{what} failed: {(r.stderr or r.stdout).decode("utf-8", "replace").strip()[-1500:]}')
    return r.stdout


def human(n): return f'{n / 2**30:.2f} GB' if abs(n) >= 2**30 else f'{n / 2**20:.0f} MB'


class Bar:
    """An in-place progress line (as build_ipa_from_nsp.py draws): open(label, total) then tick(bytes)"""
    def __init__(self): self.t0, self.erase = time.time(), 0
    def open(self, label, total):
        self.label, self.total, self.done, self.last = label, total, 0, 0.0
        return self
    def tick(self, n):
        self.done += n
        if time.time() - self.last < 2 and self.done < self.total: return
        self.last = time.time()
        pct = min(self.done, self.total) / self.total if self.total else 1
        line = f'  {self.label}  {"█" * round(pct * 20):<20} {pct:5.0%}  {human(self.done)}/{human(self.total)}  {time.time() - self.t0:.0f} s'
        print('\r' + ' ' * self.erase + '\r' + line, end='', flush=True)
        self.erase = len(line)
    def close(self, label=None):
        if self.erase: print(f'\r{" " * self.erase}\r  {label or self.label}: {human(self.done)} in {time.time() - self.t0:.0f} s', flush=True)
        self.erase = 0


def dos_t(date_time):   # a ZipInfo's date_time -> (dos time, dos date), the archive's own encoding
    y, mo, d, h, mi, s = date_time
    if y < 1980: y, mo, d, h, mi, s = 1980, 1, 1, 0, 0, 0
    return (h << 11) | (mi << 5) | (s // 2), ((y - 1980) << 9) | (mo << 5) | d


def file_dos_t(t):
    y, mo, d, h, mi, s = time.localtime(t)[:6]
    if y < 1980: y, mo, d, h, mi, s = 1980, 1, 1, 0, 0, 0
    return (h << 11) | (mi << 5) | (s // 2), ((y - 1980) << 9) | (mo << 5) | d


def from_dos(t, d):   # back to seconds, to give the extracted file the archive's timestamp (the repack compares them)
    return time.mktime((1980 + (d >> 9), (d >> 5) & 15, d & 31, t >> 11, (t >> 5) & 63, 2 * (t & 31), 0, 0, -1))


# ---- the provisioning profile and the identity ----

def load_profile(path):
    try: p = plistlib.loads(run(['security', 'cms', '-D', '-i', path], f'decode {path}'))
    except SystemExit: return None   # an unreadable profile among Xcode's is not ours to fail on
    return p if 'Entitlements' in p and 'TeamIdentifier' in p else None


def covers(profile, bundle):   # the profile's application-identifier: '<team>.<id>' exact, or '<team>.*'
    a = profile['Entitlements'].get('application-identifier', '')
    a = a[0] if isinstance(a, list) else a
    pat = a.split('.', 1)[1] if '.' in a else ''
    return 'exact' if pat.lower() == bundle.lower() else 'wildcard' if pat == '*' else None


def pick_profile(bundle):
    files = [f for d in PROFILE_DIRS for f in sorted(Path(os.path.expanduser(d)).glob('*.mobileprovision'))]
    best = None
    for f in files:
        p = load_profile(f)
        if not p or not covers(p, bundle): continue
        exp = p['ExpirationDate']
        if exp.astimezone() < datetime.now().astimezone():
            print(f'  skipped {p.get("Name", f.name)}: expired {exp.date()}'); continue
        key = (covers(p, bundle) == 'exact', bool(p['Entitlements'].get('get-task-allow')), exp)   # exact over wildcard, development over distribution, then the longest life
        if best is None or key > best[0]: best = (key, f, p)
    if best is None:
        die(f'no provisioning profile covering {bundle} in {" or ".join(PROFILE_DIRS)}. Let Xcode manage signing once for an app of that ID '
            f'(scripts/build_ipa_from_nsp.py --prepare-xcode), or pass --profile <file.mobileprovision>')
    return best[1], best[2]


def keychain_identities():   # [(sha-1, name)] of the identities that can sign
    out = run(['security', 'find-identity', '-v', '-p', 'codesigning'], 'security find-identity')
    return re.findall(r'^\s*\d+\)\s+([0-9A-Fa-f]+)\s+"(.+?)"\s*$', out.decode(), re.M)


def pick_identity(profile, ids):
    if not profile: return ids[0][1]
    certs = {hashlib.sha1(c).hexdigest().upper() for c in profile.get('DeveloperCertificates', [])}
    match = [n for h, n in ids if h.upper() in certs]
    if match: return match[0]
    die('none of the keychain signing identities is in the profile ' + str(profile.get('Name')) + ':\n  ' +
        '\n  '.join(n for _, n in ids) + '\nA cert the profile does not list makes a package no device will install: pass --identity with one the profile '
        'names, or import the profile certificate (security import <cert.p12>), or refresh the profile so it lists this Mac\'s certificate '
        '(Xcode: let automatic signing build to a device once)')


def build_entitlements(profile, bundle, extras, memory_limit):
    team = profile['TeamIdentifier'][0] if profile else None
    ent = dict(profile['Entitlements']) if profile else {}
    ent.pop('expiration-date', None)   # profile metadata, not an app entitlement
    ent.pop('beta-reports-active', None)
    if team:
        def sub(v):   # the profile's wildcards cover the team's every app; the app gets its own ID
            if isinstance(v, str): return v[:-1] + bundle if v == f'{team}.*' else v
            if isinstance(v, list): return [sub(x) for x in v]
            return v
        ent = {k: sub(v) for k, v in ent.items()}
        ent['application-identifier'] = f'{team}.{bundle}'
    if memory_limit:
        if profile and 'com.apple.developer.kernel.increased-memory-limit' not in profile['Entitlements']:
            print('  note: the profile does not provision Increased Memory Limit; a normal install may refuse the app over it (TrollStore does not care)')
        ent['com.apple.developer.kernel.increased-memory-limit'] = True
    for f in extras: ent.update(plistlib.load(open(f, 'rb')))
    return ent


# ---- extraction: the app's entries from the archive to disk (files inflate on a thread pool, timestamps and modes as stored) ----

def data_offsets(ipa, infos):   # ZipInfo -> where its compressed bytes start (the local header's own name and extra lengths)
    out = {}
    with open(ipa, 'rb') as f:
        for info in infos:
            f.seek(info.header_offset); h = f.read(30)
            if len(h) < 30 or h[:4] != b'PK\x03\x04': die(f'no local header at 0x{info.header_offset:x}: {info.filename}')
            nlen, elen = struct.unpack_from('<HH', h, 26)
            out[info] = info.header_offset + 30 + nlen + elen
    return out


def extract_app(ipa, infos, app_dir, bar):
    spans = data_offsets(ipa, infos)

    def work(info, path, mode, mt):
        if stat.S_ISLNK(mode):
            with open(ipa, 'rb') as f:
                f.seek(spans[info]); data = f.read(info.compress_size)
            if info.compress_type == 8: data = zlib.decompressobj(-15).decompress(data)
            os.symlink(data.decode('utf-8', 'surrogateescape'), path)
            os.utime(path, (mt, mt), follow_symlinks=False)
            return
        with open(ipa, 'rb') as f, open(path, 'wb') as o:
            f.seek(spans[info])
            dec = zlib.decompressobj(-15) if info.compress_type == 8 else None
            left = info.compress_size
            while left:
                b = f.read(min(CHUNK, left)); left -= len(b)
                if dec: b = dec.decompress(b)
                o.write(b)
            if dec: o.write(dec.flush())
        os.chmod(path, mode)
        os.utime(path, (mt, mt))

    pend = []
    with cf.ThreadPoolExecutor(max(4, (os.cpu_count() or 4))) as pool:
        for info in infos:
            path = app_dir / info.filename.split('/', 2)[2]
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = (info.external_attr >> 16) & 0o7777
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                if mode & 0o777: os.chmod(path, mode)
                bar.tick(0)
                continue
            fut = pool.submit(work, info, path, mode or 0o644, from_dos(*dos_t(info.date_time)))
            pend.append((fut, info.file_size))
            if len(pend) >= 512:   # a bounded window, so the pool's queue stays small
                pend[0][0].result(); bar.tick(pend.pop(0)[1])
        for fut, n in pend: fut.result(); bar.tick(n)


# ---- the new archive: unchanged entries copied raw (their compressed bytes as they are), changed ones written from disk ----

class Zip:
    def __init__(self, path):
        self.f, self.pos, self.cd = open(path, 'wb', buffering=1 << 20), 0, []
    def w(self, b):
        self.f.write(b); self.pos += len(b)
    def local(self, name, flags, method, t, d, crc, csize, usize, z64):
        nb = os.fsencode(name); fl = flags & ~0x8   # our copy carries the sizes itself, no data descriptor
        ext = struct.pack('<HHQQ', 1, 16, usize, csize) if z64 else b''
        off = self.pos
        self.w(struct.pack('<IHHHHHIIIHH', 0x04034B50, 45 if z64 else 20 if method == 8 else 10, fl, method, t, d, crc,
                           L32 if z64 else csize, L32 if z64 else usize, len(nb), len(ext)) + nb + ext)
        return off, nb, fl
    def central(self, name, h, method, t, d, crc, csize, usize, mode):
        off, nb, fl = h
        z = [v for v in (usize, csize, off) if v >= L32]   # the zip64 extra: the fields that do not fit, in this order
        ext = struct.pack(f'<HH{len(z)}Q', 1, 8 * len(z), *z) if z else b''
        self.cd.append(struct.pack('<IHHHHHHIIIHHHHHII', 0x02014B50, 0x031E, 45 if z else 20 if method == 8 else 10, fl, method, t, d, crc,
                                   min(csize, L32), min(usize, L32), len(nb), len(ext), 0, 0, 0, (mode << 16) | (0x10 if stat.S_ISDIR(mode) else 0),
                                   min(off, L32)) + nb + ext)
    def copy_raw(self, src, info, bar):   # an entry as it was: compressed bytes verbatim (method, flags, timestamp and mode from the central directory)
        if info.flag_bits & 0x1: die(f'encrypted entry, cannot copy: {info.filename}')
        z64 = info.file_size >= L32 or info.compress_size >= L32
        h = self.local(info.filename, info.flag_bits, info.compress_type, *dos_t(info.date_time), info.CRC, info.compress_size, info.file_size, z64)
        src.seek(info.header_offset); head = src.read(30)
        src.seek(info.header_offset + 30 + struct.unpack_from('<H', head, 26)[0] + struct.unpack_from('<H', head, 28)[0])
        left = info.compress_size
        while left:
            b = src.read(min(CHUNK, left)); left -= len(b)
            self.w(b); bar.tick(len(b))
        self.central(info.filename, h, info.compress_type, *dos_t(info.date_time), info.CRC, info.compress_size, info.file_size,
                     ((info.external_attr >> 16) & 0xFFFF) or 0o644)
    def put(self, name, path, st, bar):   # an entry written from disk: deflated, or stored when deflate does not shrink it
        if stat.S_ISLNK(st.st_mode):
            t = os.readlink(path).encode('utf-8', 'surrogateescape')
            h = self.local(name, 0x800 if not name.isascii() else 0, 0, *file_dos_t(st.st_mtime), zlib.crc32(t), len(t), len(t), False)
            self.w(t)
            self.central(name, h, 0, *file_dos_t(st.st_mtime), zlib.crc32(t), len(t), len(t), stat.S_IFLNK | 0o777)
            bar.tick(len(t)); return
        usize = st.st_size; z64 = usize >= L32
        for method in (8, 0):   # deflate first; stored again when the deflate did not shrink it
            h = self.local(name, 0x800 if not name.isascii() else 0, method, *file_dos_t(st.st_mtime), 0, 0 if method == 8 else usize, usize, z64)
            crc, csize, c = 0, 0, (zlib.compressobj(6, zlib.DEFLATED, -15) if method == 8 else None)
            with open(path, 'rb') as f:
                while b := f.read(CHUNK):
                    crc = zlib.crc32(b, crc)
                    if c: b = c.compress(b)
                    self.w(b); csize += len(b); bar.tick(len(b))
                if c:
                    b = c.flush(); self.w(b); csize += len(b)
            if method == 0 and csize != usize: die(f'{path} changed size while it was packed')
            if method == 8 and csize < usize: break
            self.f.seek(h[0]); self.f.truncate(); self.pos = h[0]   # not smaller: the entry restarts stored
        self.f.seek(h[0] + 14); self.f.write(struct.pack('<I', crc) + (b'' if z64 else struct.pack('<II', csize, usize)))
        if z64: self.f.seek(h[0] + 30 + len(h[1]) + 4); self.f.write(struct.pack('<QQ', usize, csize))
        self.f.seek(self.pos)
        self.central(name, h, 8 if c else 0, *file_dos_t(st.st_mtime), crc, csize, usize, st.st_mode)
    def dir(self, name, mode):   # a folder the original archive does not have (the signature's)
        h = self.local(name, 0, 0, *file_dos_t(time.time()), 0, 0, 0, False)
        self.central(name, h, 0, *file_dos_t(time.time()), 0, 0, 0, stat.S_IFDIR | (mode & 0o777 or 0o755))
    def close(self):
        cd_off, n, blob = self.pos, len(self.cd), b''.join(self.cd)
        self.w(blob)
        if n >= L16 or cd_off >= L32 or len(blob) >= L32:   # the zip64 records whenever a count, a size or an offset does not fit
            z64_eocd = self.pos
            self.w(struct.pack('<IQHHIIQQQQ', 0x06064B50, 44, 0x031E, 45, 0, 0, n, n, len(blob), cd_off))
            self.w(struct.pack('<IIQI', 0x07064B50, 0, z64_eocd, 1))
        self.w(struct.pack('<IHHHHIIH', 0x06054B50, 0, 0, min(n, L16), min(n, L16), min(len(blob), L32), min(cd_off, L32), 0))
        self.f.close()


def tree(root, arc):   # the folder's entries depth first, sorted: (archive name, path, stat); folders named with the archive's trailing slash
    with os.scandir(root) as it: es = sorted(it, key=lambda e: e.name)
    for e in es:
        name, st = f'{arc}/{e.name}', e.stat(follow_symlinks=False)
        yield (name + '/', e.path, st) if stat.S_ISDIR(st.st_mode) else (name, e.path, st)
        if stat.S_ISDIR(st.st_mode): yield from tree(e.path, name)


def repack(ipa, out, app_arc, app_dir, bar):
    """The signed archive: entries outside the app and entries codesign did not touch are copied as they were (their compressed bytes, the game's GBs
    among them); the ones it changed or added are written from disk. Returns (entries in, entries rewritten or added)."""
    with zipfile.ZipFile(ipa) as z: infos = z.infolist()
    inside = lambda n: n.startswith(app_arc + '/')
    present = {name: (p, st) for name, p, st in tree(str(app_dir), app_arc)}
    raw, fresh, claimed = 0, [], set()
    z = Zip(out + '.partial')
    try:
        with open(ipa, 'rb') as src:
            for info in infos:
                n = info.filename
                if not inside(n) or n == app_arc + '/' or n not in present:
                    if inside(n) and n != app_arc + '/': continue   # gone from the app (a stale signature): dropped
                    z.copy_raw(src, info, bar); raw += 1; continue
                _, st = present[n]
                if stat.S_ISDIR(st.st_mode):   # a folder entry: it is still there, so it stays as it was
                    z.copy_raw(src, info, bar); raw += 1; claimed.add(n); continue
                # a symlink is written from disk for its current target; a file whose size or timestamp changed (the executable, a rewritten
                # Info.plist) or that codesign added (_CodeSignature, embedded.mobileprovision) is written from disk too
                if stat.S_ISLNK(st.st_mode) or st.st_size != info.file_size or file_dos_t(st.st_mtime) != dos_t(info.date_time):
                    fresh.append(n); claimed.add(n); continue
                z.copy_raw(src, info, bar); raw += 1; claimed.add(n)
            for n in sorted(set(present) - claimed): fresh.append(n)   # codesign's new entries, chiefly _CodeSignature
            for n in fresh:
                p, st = present[n]
                if stat.S_ISDIR(st.st_mode): z.dir(n, st.st_mode)
                else: z.put(n, p, st, bar)
        z.close()
    except BaseException:
        z.f.close(); os.unlink(out + '.partial'); raise
    os.replace(out + '.partial', out)
    return len(infos), len(fresh)


def main():
    ap = argparse.ArgumentParser(description='Sign an IPA built by build_ipa_from_nsp.py and repack it, embedding a provisioning profile and entitlements')
    ap.add_argument('ipa'); ap.add_argument('--out', help='default: <ipa>-signed.ipa beside it')
    ap.add_argument('--profile', default='auto', help='a .mobileprovision to embed, or auto (the newest valid one that covers the bundle ID)')
    ap.add_argument('--identity', default='auto', help='a codesign identity by name or SHA-1, auto (the profile certificate in the keychain), or - (ad-hoc)')
    ap.add_argument('--bundle-id', help='rewrite the app bundle ID (to fit a profile made for another ID)')
    ap.add_argument('--memory-limit', action='store_true', help='add com.apple.developer.kernel.increased-memory-limit (the bigger games need it)')
    ap.add_argument('--entitlements', action='append', default=[], metavar='PLIST', help='merge these keys on top of the generated entitlements')
    ap.add_argument('--no-file-sharing', dest='file_sharing', action='store_false',
                    help='leave Info.plist as built (default: Documents visible in the Files app)')
    ap.add_argument('--work', help='the extraction folder (default: a temp folder beside the IPA)')
    ap.add_argument('--keep-work', action='store_true', help='keep the extraction folder')
    a = ap.parse_args()
    ipa = Path(a.ipa).resolve()
    if not ipa.is_file(): die(f'no IPA {ipa}')
    out = Path(a.out).resolve() if a.out else ipa.with_name(ipa.stem + '-signed.ipa')

    with zipfile.ZipFile(ipa) as z:
        apps = sorted({i.filename.split('/')[1] for i in z.infolist() if i.filename.startswith('Payload/') and i.filename.count('/') > 1})
    if len(apps) != 1: die(f'expected one app under Payload/, found {apps or "none"}')
    app_arc, app_name = f'Payload/{apps[0]}', apps[0]
    with zipfile.ZipFile(ipa) as z:
        pl = plistlib.load(z.open(f'{app_arc}/Info.plist'))
    bundle = a.bundle_id or pl['CFBundleIdentifier']
    exe = pl['CFBundleExecutable']
    print(f'{ipa.name}: {pl.get("CFBundleDisplayName", app_name)} {pl.get("CFBundleShortVersionString", "")} ({bundle}, {human(ipa.stat().st_size)})')
    if a.bundle_id and a.bundle_id != pl['CFBundleIdentifier']:
        print(f'  bundle ID rewritten to {a.bundle_id} (another ID is another app on the device, with its own saves)')

    profile_path, profile = None, None
    if a.profile != 'none' and a.identity != '-':
        if a.profile == 'auto': profile_path, profile = pick_profile(bundle)
        else:
            profile_path = Path(a.profile).expanduser()
            if not profile_path.is_file(): die(f'no profile {profile_path}')
            profile = load_profile(profile_path)
            if not profile: die(f'not an app provisioning profile: {profile_path}')
        if not covers(profile, bundle): die(f'the profile {profile.get("Name")} covers {profile["Entitlements"].get("application-identifier")}, not {bundle}')
        if profile['ExpirationDate'].astimezone() < datetime.now().astimezone(): die(f'the profile {profile.get("Name")} expired {profile["ExpirationDate"].date()}')
        print(f'  profile: {profile.get("Name")} ({profile["TeamIdentifier"][0]}, expires {profile["ExpirationDate"].date()})')
    ids = keychain_identities()
    if a.identity == '-': identity = '-'
    elif a.identity == 'auto': identity = pick_identity(profile, ids)
    else:
        identity = next((n for h, n in ids if a.identity in n or h.upper().startswith(a.identity.upper())), None) or die(
            f'no signing identity matching "{a.identity}":\n  ' + '\n  '.join(n for _, n in ids))
    ent = build_entitlements(profile, bundle, a.entitlements, a.memory_limit)
    if profile and identity != '-':   # a signature by a certificate the profile does not list is valid yet uninstalled by any device (the 12:17 package)
        certs = {hashlib.sha1(c).hexdigest().upper() for c in profile.get('DeveloperCertificates', [])}
        leaf = next((h for h, n in ids if n == identity), None)
        if leaf and leaf.upper() not in certs:
            die(f'the identity "{identity}" is not among the certificates of the profile {profile.get("Name")} — iOS refuses to install such a package. '
                f'Use a certificate the profile lists, or a profile that lists this certificate')

    work = Path(a.work).resolve() if a.work else Path(tempfile.mkdtemp(prefix=f'.{ipa.stem}-sign-', dir=ipa.parent))
    work.mkdir(parents=True, exist_ok=True)
    app_dir = work / app_name
    try:
        with zipfile.ZipFile(ipa) as z:
            infos = [i for i in z.infolist() if i.filename == app_arc + '/' or i.filename.startswith(app_arc + '/')]
        infos.sort(key=lambda i: i.header_offset)   # parents first, as packed
        total = sum(i.file_size for i in infos)
        free = shutil.disk_usage(work).free
        if free < total + ipa.stat().st_size: die(f'{human(free)} free on {work.parent}, for {human(total)} of app plus {human(ipa.stat().st_size)} of new IPA')
        print(f'  extract the app ({human(total)}; work {work}' + (', kept' if a.keep_work else ', deleted') + ' afterwards)')
        bar = Bar().open('extract', total)
        extract_app(ipa, infos, app_dir, bar)
        bar.close()
        shutil.rmtree(app_dir / '_CodeSignature', ignore_errors=True)   # codesign --force rewrites it; a stale one must not stay
        with open(app_dir / 'Info.plist', 'rb') as f: pl = plistlib.load(f)
        edits = []
        if a.bundle_id and a.bundle_id != pl['CFBundleIdentifier']:
            pl['CFBundleIdentifier'] = a.bundle_id; edits.append(f'bundle ID → {a.bundle_id}')
        if a.file_sharing:
            for k in ('UIFileSharingEnabled', 'LSSupportsOpeningDocumentsInPlace'):
                if pl.get(k) is not True: pl[k] = True; edits.append(f'{k} on')
        if edits:
            with open(app_dir / 'Info.plist', 'wb') as f: plistlib.dump(pl, f)
            print('  Info.plist: ' + ', '.join(edits))
        elif a.file_sharing:
            print('  Info.plist: file sharing already on (Documents in the Files app)')
        if profile_path: shutil.copyfile(profile_path, app_dir / 'embedded.mobileprovision')
        ent_path = work / 'entitlements.plist'
        with open(ent_path, 'wb') as f: plistlib.dump(ent, f)
        print(f'  sign: {identity}' + (f' ({profile["TeamIdentifier"][0]})' if profile else ' (ad-hoc, for TrollStore)') +
              ', entitlements: ' + (' '.join(sorted(ent)) if ent else 'none'))
        for sub in ('PlugIns', 'Frameworks', 'XPCServices'):   # none of these in this app; kept for a bundle that grows one (signed first, bottom up)
            if (app_dir / sub).is_dir():
                for child in sorted((app_dir / sub).iterdir()):
                    run(['codesign', '--force', '--generate-entitlement-der', '--entitlements', ent_path, '--sign', identity, child], f'codesign {child.name}')
        run(['codesign', '--force', '--generate-entitlement-der', '--entitlements', ent_path, '--sign', identity, app_dir], 'codesign')
        run(['codesign', '--verify', '--strict', app_dir], 'codesign --verify')

        bar = Bar().open('pack', ipa.stat().st_size)
        n_entries, n_fresh = repack(ipa, str(out), app_arc, app_dir, bar)
        bar.close('pack (unchanged entries copied as they were)')
    finally:
        if a.keep_work: print(f'  work kept: {work}')
        else: shutil.rmtree(work, ignore_errors=True)

    with zipfile.ZipFile(out) as z:   # the archive it wrote reads back and holds the signature
        z.read(f'{app_arc}/_CodeSignature/CodeResources')
        names = z.namelist()
    if profile_path and f'{app_arc}/embedded.mobileprovision' not in names: die('the packed IPA holds no embedded.mobileprovision')
    print(f'signed: {out} ({human(out.stat().st_size)}, {n_entries} entries, {n_fresh} rewritten or added)')
    print(f'  install: Apple Configurator or Finder, or: xcrun devicectl device install app --device <UDID> "{out}"')


if __name__ == '__main__': main()
