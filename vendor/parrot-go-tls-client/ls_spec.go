package main

// LS 指纹规格：从 Antigravity Hub 2.22.0 language_server 的真实 ClientHello
// 逆向而来（证据: antigravity-rev/mitm/clienthellos/*.bin, 2026-10-12 实测）。
// 目标是字节级复刻: cipher 顺序、扩展顺序（含 ext 50）、全部 PQ 曲线组、
// Google 私有 sigalgs(0x0904-0x0906)、MLKEM key_share、无 session ticket。

import (
	"crypto/tls"

	tlsu "github.com/refraction-networking/utls"
)

// lsCipherSuites: TLS1.2 套件前置、CBC SHA1 保留、TLS1.3 垫后（LS 实测顺序）
var lsCipherSuites = []uint16{
	0xc02b, 0xc02f, 0xc02c, 0xc030, 0xcca9, 0xcca8,
	0xc009, 0xc013, 0xc00a, 0xc014,
	0x1301, 0x1302, 0x1303,
}

// lsSigAlgs: Google 内部 Go 的签名算法表（含私有 ML-DSA 0x0904-0x0906）
var lsSigAlgs = []tlsu.SignatureScheme{
	0x0904, 0x0905, 0x0906,
	0x0804, 0x0403, 0x0807,
	0x0805, 0x0806,
	0x0401, 0x0501, 0x0601,
	0x0503, 0x0603,
}

// lsSigAlgsCert: ext 50 内容 = sigalgs + SHA1 兼容回退（实测 15 项）
var lsSigAlgsCert = append(append([]tlsu.SignatureScheme{}, lsSigAlgs...), 0x0201, 0x0203)

func lsClientHelloSpec() *tlsu.ClientHelloSpec {
	return &tlsu.ClientHelloSpec{
		TLSVersMin:         tls.VersionTLS12,
		TLSVersMax:         tls.VersionTLS13,
		CompressionMethods: []byte{0},
		CipherSuites:       lsCipherSuites,
		Extensions: []tlsu.TLSExtension{
			&tlsu.SNIExtension{},
			&tlsu.SupportedPointsExtension{SupportedPoints: []byte{0}},
			&tlsu.RenegotiationInfoExtension{Renegotiation: tlsu.RenegotiateOnceAsClient},
			&tlsu.ExtendedMasterSecretExtension{},
			&tlsu.SCTExtension{},
			&tlsu.StatusRequestExtension{},
			&tlsu.SupportedCurvesExtension{Curves: []tlsu.CurveID{
				tlsu.X25519MLKEM768, // 4588
				tlsu.CurveID(4587),  // X25519Kyber768Draft00（保持 wire 值）
				tlsu.CurveID(4589),  // SecP256r1MLKEM768（仅声明，无 key_share）
				tlsu.X25519,         // 29
				tlsu.CurveP256,      // 23
				tlsu.CurveP384,      // 24
				tlsu.CurveP521,      // 25
			}},
			&tlsu.SignatureAlgorithmsExtension{SupportedSignatureAlgorithms: lsSigAlgs},
			&tlsu.SignatureAlgorithmsCertExtension{SupportedSignatureAlgorithms: lsSigAlgsCert},
			&tlsu.ALPNExtension{AlpnProtocols: []string{"h2", "http/1.1"}},
			&tlsu.SupportedVersionsExtension{Versions: []uint16{
				tls.VersionTLS13, tls.VersionTLS12,
			}},
			&tlsu.KeyShareExtension{KeyShares: []tlsu.KeyShare{
				{Group: tlsu.X25519MLKEM768},
				{Group: tlsu.X25519},
			}},
		},
	}
}
