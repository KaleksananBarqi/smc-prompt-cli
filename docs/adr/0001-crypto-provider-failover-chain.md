# Crypto Provider Failover Chain & Multi-Exchange Support

Guna mengatasi kegagalan jaringan dan pemblokiran ISP lokal terhadap API publik Binance Futures, sistem menerapkan rantai pengalihan otomatis (*Crypto Failover Chain*) dengan urutan prioritas Binance ➔ Bitunix ➔ MEXC ➔ Gate.io serta opsi penonaktifan tegas (`--no-fallback`).

## Status
Accepted

## Konteks
Banyak pengguna di wilayah tertentu (seperti Indonesia) mengalami pemblokiran DNS/koneksi (TrustPositif/Internet Baik) terhadap domain `fapi.binance.com`, yang sebelumnya menyebabkan `smc-prompt` terhenti dengan `NetworkError` tanpa alternatif. Di saat yang sama, data pasar derivatif USDT-M Perpetual Futures juga tersedia secara publik dan gratis tanpa API key di Bitunix, MEXC, dan Gate.io.

## Keputusan
1. Mengintegrasikan dua data provider baru: **MEXC Futures** (`api.mexc.com`) dan **Gate.io Futures** (`api.gateio.ws`), melengkapi Binance dan Bitunix.
2. Membentuk urutan pengalihan (*failover chain*): `binance` ➔ `bitunix` ➔ `mexc` ➔ `gate`.
3. Memicu failover hanya pada `NetworkError` (koneksi terputus, timeout, HTTP 403/451 geo-block). Kesalahan input simbol (`SymbolNotFoundError`) atau argumen (`ConfigError`) tidak memicu failover.
4. Menjaga atomisitas data (*clean cutover*): jika terjadi kegagalan jaringan di tengah proses pengambilan data, proses pada exchange tersebut dibatalkan dan seluruh pipeline data diulang dari awal pada exchange berikutnya.
5. Menyediakan opsi `--no-fallback` untuk pengujian ketat (*Strict Provider Mode*).
6. Memperbarui Interactive Wizard dengan menambahkan pilihan MEXC dan Gate.io.

## Konsekuensi
- CLI dan Interactive Wizard memiliki ketahanan tinggi terhadap pemblokiran jaringan ISP lokal.
- Output prompt Markdown selalu mencatat exchange aktual yang berhasil ditarik datanya pada header `Provider: ...`.
