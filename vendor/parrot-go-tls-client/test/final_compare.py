#!/usr/bin/env python3
"""终验：parrot-go-tls-client 输出 vs LS 真实样本，ClientHello + h2 首帧逐字段对比。"""
import argparse
import glob
from pathlib import Path
import hashlib



def parse_clienthello(buf):
    rl = int.from_bytes(buf[3:5], 'big')
    rec = buf[5:5 + rl]
    p = 4 + 2 + 32
    sid_len = rec[p]; p += 1 + sid_len
    cs_len = int.from_bytes(rec[p:p + 2], 'big'); p += 2
    ciphers = [int.from_bytes(rec[p + i:p + i + 2], 'big') for i in range(0, cs_len, 2)]
    p += cs_len
    cm_len = rec[p]; p += 1 + cm_len
    ext_len = int.from_bytes(rec[p:p + 2], 'big'); p += 2
    order, exts = [], {}
    end = p + ext_len
    while p + 4 <= min(end, len(rec)):
        et = int.from_bytes(rec[p:p + 2], 'big'); el = int.from_bytes(rec[p + 2:p + 4], 'big'); p += 4
        order.append(et); exts[et] = rec[p:p + el]; p += el
    return ciphers, order, exts


def sig_list(raw):
    n = int.from_bytes(raw[:2], 'big')
    return [f"{int.from_bytes(raw[2 + i:4 + i], 'big'):04x}" for i in range(0, n, 2)]


def curves_of(exts):
    e = exts[10]; n = int.from_bytes(e[:2], 'big')
    return [int.from_bytes(e[2 + i:4 + i], 'big') for i in range(0, n, 2)]


def keyshare_of(exts):
    e = exts.get(51, b''); p, out = 2, []
    while p + 4 <= len(e):
        g = int.from_bytes(e[p:p + 2], 'big'); l = int.from_bytes(e[p + 2:p + 4], 'big')
        out.append((g, l)); p += 4 + l
    return out


def formats_of(exts):
    raw = exts.get(11, b"\x00")
    if len(raw) != 1 + raw[0]:
        raise ValueError("invalid EC point formats vector")
    return list(raw[1:])


def ja3(ciphers, order, curves, fmts):
    s = f"771,{'-'.join(map(str, ciphers))},{'-'.join(map(str, order))},{'-'.join(map(str, curves))},{'-'.join(map(str, fmts))}"
    return s, hashlib.md5(s.encode()).hexdigest()


def parse_h2_first_frames(data):
    pre = b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
    if data.startswith(pre):
        data = data[len(pre):]
    p, frames = 0, []
    while p + 9 <= len(data):
        ln = int.from_bytes(data[p:p + 3], 'big'); ft = data[p + 3]; flags = data[p + 4]
        payload = data[p + 9:p + 9 + ln]
        detail = ''
        if ft == 4 and len(payload) >= 6:
            items = [f"{int.from_bytes(payload[i:i+2],'big'):x}={int.from_bytes(payload[i+2:i+6],'big')}"
                     for i in range(0, len(payload) - 5, 6)]
            detail = ' '.join(items)
        elif ft == 8 and len(payload) == 4:
            detail = f"inc={int.from_bytes(payload, 'big')}"
        frames.append((ft, ln, flags, detail))
        p += 9 + ln
    return frames


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    base = Path("/opt/src-space/antigravity-rev/mitm")
    parser.add_argument("--ls-ch", default=str(base / "clienthellos/antigravity-unleash.goog_52006.bin"))
    parser.add_argument("--ls-h2", default=str(base / "h2frames/antigravity-unleash.goog_h2_6654.bin"))
    parser.add_argument("--pg-ch")
    parser.add_argument("--pg-h2")
    args = parser.parse_args()
    def latest(pattern):
        files = sorted(glob.glob(pattern))
        if not files:
            parser.error(f"no sample found: {pattern}; supply an explicit path")
        return files[-1]
    LS_CH, LS_H2 = args.ls_ch, args.ls_h2
    PG_CH = args.pg_ch or latest(str(base / "clienthellos/pgtc-verify*.bin"))
    PG_H2 = args.pg_h2 or latest(str(base / "h2frames/pgtc-verify*.bin"))
    ls_c, ls_o, ls_e = parse_clienthello(open(LS_CH, 'rb').read())
    pg_c, pg_o, pg_e = parse_clienthello(open(PG_CH, 'rb').read())

    checks = []


    def check(name, a, b):
        ok = a == b
        checks.append((name, ok, a, b))
        return ok


    check('cipher 列表+顺序', pg_c, ls_c)
    check('扩展顺序', pg_o, ls_o)
    check('curves(含PQ组)', curves_of(pg_e), curves_of(ls_e))
    check('sigalgs(ext13)', sig_list(pg_e[13]), sig_list(ls_e[13]))
    check('sigalgs_cert(ext50)', sig_list(pg_e[50]) if 50 in pg_e else None, sig_list(ls_e[50]))
    check('key_share', keyshare_of(pg_e), keyshare_of(ls_e))
    ls_ja3 = ja3(ls_c, ls_o, curves_of(ls_e), formats_of(ls_e))[1]
    pg_ja3 = ja3(pg_c, pg_o, curves_of(pg_e), formats_of(pg_e))[1]
    check('JA3 哈希', pg_ja3, ls_ja3)
    check('session_ticket 缺席(与LS一致)', 35 in pg_o, 35 in ls_o)

    ls_h2 = parse_h2_first_frames(open(LS_H2, 'rb').read())
    pg_h2 = parse_h2_first_frames(open(PG_H2, 'rb').read())
    check('h2 首帧序列(SETTINGS+WINDOW_UPDATE)', pg_h2, ls_h2)

    print('=' * 72)
    allok = True
    for name, ok, a, b in checks:
        mark = '✅' if ok else '❌'
        allok &= ok
        print(f"{mark} {name}")
        if not ok:
            print(f"     ours: {a}")
            print(f"     LS  : {b}")
    print('=' * 72)
    print("JA3:", pg_ja3, "(LS:", ls_ja3 + ")")
    print("h2 帧:", pg_h2)
    print()
    print("结论:", "所比较初始字段一致；业务host和动态行为等价性未确认" if allok else "存在差异，见上")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
