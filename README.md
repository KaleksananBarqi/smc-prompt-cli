# smc-prompt

`smc-prompt` adalah **CLI Python sekali-jalan (single-run)** yang mengambil data
OHLC dari **provider market data publik read-only** (Binance Spot secara default;
Twelve Data atau OANDA untuk FX/logam), menghitung **fakta struktural yang
objektif dan mekanis** (lapisan `[FAKTA]`), menyuntikkan fakta tersebut ke dalam
template prompt yang tetap, lalu **menulis prompt akhir ke berkas `.md` di
`./output`**, **menyalinnya ke clipboard** (best-effort), dan opsional
**mencetaknya ke terminal** lewat `--stdout`.

Alat ini adalah **utilitas penyiapan data (data-preparation utility)** untuk LLM
teks di hilir. Ia **tidak melakukan penalaran apa pun** sendiri; seluruh
interpretasi non-trivial (DOL, liquidity sweep, bias, konfirmasi entry)
didelegasikan sepenuhnya kepada LLM yang membaca prompt.

> Dokumentasi desain lengkap dan beku (frozen) tersedia di
> [`docs/DESIGN_SPEC.md`](docs/DESIGN_SPEC.md).

---

## Mengapa Repositori Ini Dibuat?

Ada tiga alasan praktis di balik `smc-prompt`:

1. **Murah — SMC murni cukup dengan data publik.** Analisis Smart Money
   Concepts yang mekanis (swing, struktur, equal highs/lows, FVG) hanya
   membutuhkan **OHLCV**. Tidak ada kebutuhan data eksotis seperti order flow,
   footprint, likuiditas berbayar, atau feed institusional. **Binance public
   REST API** menyediakan seluruh data yang diperlukan **tanpa API key** dan
   **tanpa biaya** — cukup menarik data publik, selesai.

2. **Tanpa API AI berbayar.** Tool ini **tidak memanggil LLM API apa pun** dan
   tidak mengunci Anda ke penyedia AI berbayar (lihat "Non-Goals"). Ia hanya
   menyiapkan prompt akhir berbasis fakta mekanis; Anda cukup **mem-paste**
   prompt tersebut ke AI pilihan Anda — **termasuk layanan AI gratis**.

3. **Sederhana.** Satu CLI sekali-jalan: satu perintah mengambil data,
   menghitung fakta mekanis, merender prompt, lalu menulis berkas `.md` (plus
   salin clipboard). Tanpa server, tanpa database, tanpa konfigurasi berat.

Singkatnya: **data murah, tanpa biaya AI, dan alur sesederhana mungkin** — Anda
tinggal menjalankan satu perintah lalu menempelkan hasilnya ke LLM mana pun.

---

## Fitur Utama

- **Dua lapisan payload** yang disuntikkan ke prompt:
  - **Layer A — Computed Summary:** harga saat ini, swing high/low terdeteksi
    (harga + timestamp), **sequence swing berlabel** (HH/HL/LH/LL/EQH/EQL),
    klasifikasi struktur mekanis, **equal highs/lows (liquidity pool)**,
    **Fair Value Gap (FVG) mekanis** (3-candle, lengkap dengan status
    `filled`/`unfilled`), **tiga jenis referensi level** (recent / nearest /
    window-extreme) lengkap dengan status `swept`/`untested`, metrik jarak, dan
    ATR(14) opsional. Jarak swing juga disajikan **ternormalisasi ATR**
    (`x{n}×ATR`), ATR(14) diringkas sebagai **persentase harga**
    (`ATR ≈ x% of price`), dan candle terakhir diberi **sinyal volume mekanis**
    (volume relatif `last/mean(N)` dan flag spike). Lihat Phase 5 di bawah.
  - **Layer B — Raw Candle Table:** tabel OHLCV ringkas format CSV untuk **tiga
    timeframe native** (HTF, MTF, LTF; default `1d` / `4h` / `1h`, tiap interval
    dapat dikonfigurasi) agar LLM dapat menurunkan sendiri Order Block dan
    CHoCH/MSS (beberapa swing). Ketiga interval harus **berbeda (distinct)**.
    FVG kini dihitung di Layer A sebagai fakta mekanis murni (lihat di bawah).
- **Deteksi swing deterministik:** N-bar Williams Fractal (default `N = 5`)
  dengan post-filter pemisahan ATR dan **post-pass skeleton bergantian**
  (`enforce_alternation`) yang menjamin urutan H/L/H/L murni. Tanpa random,
  tanpa rekursi, tanpa lookahead.
- **Klasifikasi struktur mekanis:** `Bullish` / `Bearish` / `Ranging/Mixed` /
  `Equal Highs/Lows` — murni perbandingan numerik swing terakhir, **bukan**
  "bias".
- **Retry + backoff** dan **failover host** (connection error, HTTP 451
  geo-block, HTTP 403).
- **Waktu server Binance** (`GET /api/v3/time`) sebagai acuan `now` untuk
  keputusan candle closed dan `GENERATED_AT_UTC`, sehingga jam host yang miring
  tidak bisa menyuntikkan candle setengah-terbentuk. Bila endpoint gagal, jatuh
  ke jam host dengan peringatan.
- **Presisi harga dari `PRICE_FILTER.tickSize`** (`exchangeInfo`) — jumlah
  desimal mengikuti tickSize simbol (mis. `0.01000000` → 2 dp), dengan
  fallback berbasis magnitudo bila tick tidak tersedia.
- **Peringatan non-fatal** untuk status simbol non-`TRADING` dan untuk harga
  saat ini yang tidak wajar (non-positif atau di luar rentang
  `[low, high]` candle closed terakhir ± 1×ATR).
- **Output berbasis berkas (default):** prompt **selalu** ditulis ke
  `./output/<SYMBOL>-<YYYY-MM-DD-HH-MM-SS-UTC>.md` lalu disalin ke clipboard (best-effort);
  `--stdout` menambahkan cetak ke stdout. Jika clipboard tidak tersedia
  (lingkungan headless), itu hanya peringatan (exit 0).
- **Tiga timeframe native (3-tier)** — satu kali jalan mengambil, menganalisis,
  dan merender **tiga seri** (HTF + MTF + LTF; default `1d` / `4h` / `1h`) ke
  dalam satu prompt. Tier MTF adalah instance ketiga melalui pipeline
  `analyze()` yang sama; `TimeframeAnalysis` tidak berubah.
- **Mode offline / data lokal (Phase 4)** — `--input-csv` (opsional
  `--htf-file` / `--mtf-file` / `--ltf-file`) menjalankan tool **tanpa akses
  jaringan sama sekali** dari berkas CSV OHLCV lokal, dengan keluaran
  **deterministik** (bisa meregenerasi/meninjau perubahan template tanpa candle
  live). Jalur jaringan tetap menjadi **default**.
- **Guard ukuran prompt (Phase 5, #11)** — setelah render, ukuran prompt (byte
  UTF-8) diperiksa: melewati ambang lunak `PROMPT_BYTES_WARN` (default
  `120000`) memunculkan `WARN` berisi jumlah byte + perkiraan token; flag
  opsional `--max-prompt-bytes` menjadi batas keras yang membatalkan dengan
  `ConfigError` (exit `2`) sebelum berkas ditulis.
- **`--dry-run` (Phase 5, #16)** — validasi konfigurasi + simbol lalu cetak
  pengaturan yang diresolusi ke **stderr**, tanpa fetch klines, render, atau
  menulis berkas (cocok untuk CI/pre-flight).
- **Dukungan XAUUSD / FX-logam (Phase 6)** — Binance Spot **tidak melisting**
  instrumen fiat/forex/logam, sehingga XAUUSD tidak bisa diambil dari Binance.
  Provider `twelvedata` (key gratis, **4h native**) dan `oanda` (akun practice
  gratis, spot `XAU_USD` asli, `H4` native) menyajikan pair/logam sungguhan.
  Pipeline analisis **tidak berubah**: provider hanyalah sumber data lain dengan
  kontrak yang sama (`validate_symbol` / `fetch_klines` / `fetch_current_price` /
  `fetch_server_time` / `price_notes` / `with_now`).
- **Tanpa API key untuk Binance**, hanya endpoint **read-only public market data**
  (provider FX opsional memerlukan key/token; lihat bagian XAUUSD di bawah).

## Non-Goals (batas keras)

- **Tidak ada panggilan LLM API apa pun.**
- **Tidak ada penalaran SMC/ICT** — tidak menghitung DOL, tidak menarasikan
  liquidity sweep, tidak menetapkan trading bias.
- **Tidak ada pemrosesan gambar/vision.**
- **Tidak ada eksekusi order / auto-trading.**
- Hanya endpoint **read-only public** Binance market data yang dipanggil.

> **Catatan (FVG — Phase 2).** Deteksi **Fair Value Gap (FVG) mekanis** kini
> **diizinkan** karena FVG adalah **celah harga 3-candle yang murni mekanis dan
> dapat diturunkan langsung dari OHLC** (bullish bila `low[i] > high[i-2]`,
> bearish bila `high[i] < low[i-2]`) — tanpa interpretasi apa pun. Karenanya FVG
> masuk ke lapisan `[FAKTA]`, sama seperti fakta swing/equal-level mekanis.
> Hal ini **tidak** mengubah non-goal lainnya: DOL, liquidity sweep, dan bias
> tetap didelegasikan ke LLM. Order Block dan CHoCH/MSS juga tetap diturunkan
> oleh LLM. Lihat [`docs/DESIGN_SPEC.md`](docs/DESIGN_SPEC.md) §2 (amendemen) dan
> §4.7.

## Persyaratan (Requirements)

- **Python 3.10 atau lebih baru** (`requires-python = ">=3.10"`).
- Sistem operasi: Windows / macOS / Linux.
- Koneksi internet ke endpoint publik Binance.
- Dependensi Python (lihat [`requirements.txt`](requirements.txt)):
  - [`requests`](https://pypi.org/project/requests/) `>= 2.31.0`
  - [`pandas`](https://pypi.org/project/pandas/) `>= 2.0.0`
  - [`pyperclip`](https://pypi.org/project/pyperclip/) `>= 1.8.2`
  - [`click`](https://pypi.org/project/click/) `>= 8.1.7`
  - [`jinja2`](https://pypi.org/project/jinja2/) `>= 3.1.2`
  - [`python-dotenv`](https://pypi.org/project/python-dotenv/) `>= 1.0.0`
    (membaca berkas `.env` kredensial opsional; lihat "Kredensial via `.env`")
- Catatan clipboard: di Linux headless, `pyperclip` membutuhkan `xclip`/`xsel`.
  Bila tidak ada, penyalinan dilewati dengan peringatan; berkas `.md` di
  `--output-dir` tetap selalu tertulis (exit code tetap `0`).

## Instalasi

```bash
# 1. Clone repositori
git clone <URL_REPO_ANDA> smc-prompt
cd smc-prompt

# 2. Buat dan aktifkan virtualenv
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 3. Pasang dependensi
pip install -r requirements.txt

# 4. Pasang paket dalam mode editable (opsional, untuk console script)
pip install -e .

# 5. (Opsional) Pasang extra dev untuk menjalankan test suite
pip install -e ".[dev]"   # menambahkan pytest
```

Setelah `pip install -e .`, skrip konsol `smc-prompt` tersedia di `PATH`.
Paket juga dapat dijalankan tanpa instalasi melalui `python -m smc_prompt`.

## Cara Penggunaan (Usage)

Bentuk umum:

```
smc-prompt <SYMBOL> [--htf-interval I] [--mtf-interval I] [--ltf-interval I]
                     [--htf-candles N] [--mtf-candles N] [--ltf-candles N]
                     [--swing-lookback N]
                     [--distance-reference {nearest,most-recent}] [--no-atr]
                     [--base-url URL] [--output-dir PATH] [--stdout]
                     [--input-csv FILE] [--htf-file FILE] [--mtf-file FILE] [--ltf-file FILE]
                     [--max-prompt-bytes BYTES] [--dry-run]
                     [--env-file PATH] [--no-dotenv] [--debug]
```

Entry point yang tersedia:

- `smc-prompt <SYMBOL> ...` — console script dari `pip install -e .`
  ([`pyproject.toml`](pyproject.toml)).
- `python -m smc_prompt <SYMBOL> ...` — tanpa instalasi
  ([`smc_prompt/__main__.py`](smc_prompt/__main__.py)).

Bantuan dan versi:

```bash
smc-prompt --help      # atau -h
smc-prompt --version
```

### Penjelasan Opsi / Flag CLI

Tabel berikut diturunkan langsung dari [`smc_prompt/cli.py`](smc_prompt/cli.py)
dan [`smc_prompt/config.py`](smc_prompt/config.py).

| Flag / Argumen | Tipe | Default | Keterangan |
|---|---|---|---|
| `SYMBOL` | positional `str` | — | Simbol Binance Spot, mis. `BTCUSDT`. Case-insensitive; dinormalisasi ke huruf besar. Wajib diisi. |
| `--htf-interval` | interval Binance | `1d` | Interval kline Binance untuk seri HTF. Nilai valid: `1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1M`. Nilai di luar daftar ditolak dengan exit code `2`. |
| `--mtf-interval` | interval Binance | `4h` | Interval kline Binance untuk seri MTF (medium). Daftar nilai valid sama dengan `--htf-interval`. Harus berbeda dari dua tier lainnya. |
| `--ltf-interval` | interval Binance | `1h` | Interval kline Binance untuk seri LTF. Daftar nilai valid sama dengan `--htf-interval`. Harus berbeda dari dua tier lainnya. |
| `--htf-candles` | `int >= 10` | `60` | Jumlah candle **HTF-interval tertutup** (closed) pada tabel mentah HTF. |
| `--mtf-candles` | `int >= 10` | `120` | Jumlah candle **MTF-interval tertutup** (closed) pada tabel mentah MTF. |
| `--ltf-candles` | `int >= 10` | `100` | Jumlah candle **LTF-interval tertutup** (closed) pada tabel mentah LTF. |
| `--swing-lookback` | `int` ganjil `>= 3` | `5` | Ukuran window fractal `N` untuk deteksi swing. |
| `--distance-reference` | pilihan: `nearest` \| `most-recent` | `nearest` | Referensi swing untuk perhitungan jarak. `nearest` = swing terdekat berdasarkan jarak harga absolut ke harga saat ini; `most-recent` = swing terbaru berdasarkan timestamp. |
| `--no-atr` | flag | off (ATR aktif) | Menghilangkan baris ATR(14) dari prompt. Diimplementasikan lewat blok Jinja `{% if INCLUDE_ATR %}` di template (bukan pemotongan baris berbasis string). Perhitungan ATR tetap dilakukan (dipakai filter swing); hanya tampilan yang disembunyikan. |
| `--base-url` | `str` (URL) | `None` | Override host REST Binance. Host ini di-`prepend` ke daftar host default, tetap dengan failover. |
| `--output-dir` | `path` (folder) | `output` | Direktori untuk berkas prompt `.md` yang dihasilkan. |
| `--stdout` | flag | off | Selain menulis berkas `.md`, cetak juga prompt ke stdout. |
| `--input-csv` | `path` (file) | — | **Mode offline (Phase 4).** Baca candle OHLCV dari CSV lokal, bukan dari Binance. Memasok **ketiga** timeframe kecuali di-override `--htf-file`/`--mtf-file`/`--ltf-file`. Jalur jaringan tetap default; mode offline hanya aktif bila flag ini diberikan. Lihat bagian "Mode Offline (Data Lokal CSV)". |
| `--htf-file` | `path` (file) | — | CSV candle HTF untuk mode offline. **Wajib** disertai `--input-csv`; hanya menimpa seri HTF. |
| `--mtf-file` | `path` (file) | — | CSV candle MTF untuk mode offline. **Wajib** disertai `--input-csv`; hanya menimpa seri MTF. |
| `--ltf-file` | `path` (file) | — | CSV candle LTF untuk mode offline. **Wajib** disertai `--input-csv`; hanya menimpa seri LTF. |
| `--max-prompt-bytes` | `int` | — (nonaktif) | **Batas keras ukuran prompt (Phase 5).** Bila prompt hasil render melebihi jumlah byte ini, tool membatalkan dengan `ConfigError` (exit code `2`) **sebelum** menulis berkas. Nonaktif secara default. |
| `--dry-run` | flag | off | **Dry run (Phase 5).** Validasi konfigurasi + simbol lalu cetak pengaturan yang diresolusi ke **stderr**, tanpa fetch klines, render, atau menulis berkas. |
| `--candles-only` (atau `--review`) | flag | off | **Mode Post-Trade Review.** Hanya mengekspor data candle mentah (OHLCV) dan ringkasan pergerakan harga sesi ke berkas `.md` tanpa menyertakan template prompt analisis SMC pre-trade. |
| `--review-interval` | interval Binance | `1h` | Interval candle untuk mode review (misal `15m`, `1h`, `4h`). |
| `--review-candles` | `int` (5–500) | `30` | Jumlah candle closed yang diekspor pada mode review. |
| `--validate-setup` (atau `--check-setup`) | flag | off | **Validasi Setup & Deteksi Front-Run.** Menghasilkan prompt terfokus untuk mengevaluasi apakah limit order/setup yang direncanakan masih valid, sudah ter-front-run, tersapu targetnya, atau sudah terjemput. |
| `--entry` | `float` | — | Level harga entry yang direncanakan (wajib untuk `--validate-setup`). |
| `--tp` | `float` | — | Level harga Take Profit / target DOL yang direncanakan (wajib untuk `--validate-setup`). |
| `--sl` | `float` | `None` | Level harga Stop Loss yang direncanakan (opsional untuk `--validate-setup`). |
| `--direction` | pilihan: `long` \| `short` | otomatis | Arah posisi (`long`/`short`). Bila tidak diisi, otomatis disimpulkan dari perbandingan `entry` dan `tp`. |
| `--validate-interval` | interval Binance | `15m` | Interval candle LTF untuk melacak riwayat pendekatan harga (misal `15m`, `5m`, `1h`). |
| `--validate-candles` | `int` (5–500) | `50` | Jumlah candle closed yang dianalisis pada mode validasi setup. |
| `--env-file` | `path` (file) | `.env` | **Kredensial (Phase 6).** Berkas `.env` tempat kredensial provider dibaca. Tidak pernah menimpa variabel yang sudah ada di shell (prioritas: flag > environment > `.env`). Lihat "Kredensial via `.env`". |
| `--no-dotenv` | flag | off | **Kredensial (Phase 6).** Lewati pemuatan `.env` sepenuhnya. Berguna untuk CI dan debugging agar berkas lokal tidak diam-diam mengubah hasil run. |
| `--debug` | flag | off | Cetak stack trace saat error. |
| `--version` | flag | — | Tampilkan versi program lalu keluar. |
| `-h`, `--help` | flag | — | Tampilkan bantuan lalu keluar. |

### Semantik `--distance-reference`

- `nearest` (default): swing high/low yang dilaporkan adalah swing terdeteksi
  yang **harganya paling dekat secara absolut** dengan harga saat ini.
- `most-recent`: swing high/low yang dilaporkan adalah **swing terdeteksi
  terbaru berdasarkan timestamp**.

Kedua mode terimplementasi penuh; default adalah `nearest`.

### Failover Host

CLI mencoba host secara berurutan dan berpindah (failover) saat connection
error, HTTP 451 (geo-block), dan HTTP 403:

1. `https://api.binance.com`
2. `https://data-api.binance.vision`

Memberikan `--base-url` akan menempatkan host tersebut di urutan pertama,
diikuti host fallback yang tersisa (host yang sama tidak diduplikasi). Daftar
host tersimpan di [`smc_prompt/config.py`](smc_prompt/config.py) sebagai
konstanta `DEFAULT_BASE_URLS`.

### Format Output

- Prompt yang dirender **selalu** ditulis ke
  `output/<SYMBOL>-<YYYY-MM-DD-HH-MM-SS-UTC>.md`, mis.
  `output/BTCUSDT-2026-09-13-08-09-34-UTC.md` — berkas ini adalah artefak utama.
  Stamp selalu dinormalisasi ke **UTC** dan memakai pemisah tanda hubung
  (tanpa titik dua) sehingga aman di filesystem Windows maupun POSIX.
- Setelah berkas tertulis, prompt disalin ke **clipboard** secara **best-effort**.
  Bila clipboard tidak tersedia (lingkungan headless), hanya peringatan yang
  dikirim ke stderr dan exit code tetap `0`.
- **stdout kosong secara default**; prompt hanya dicetak ke stdout bila Anda
  memakai flag `--stdout`. Seluruh catatan, peringatan, dan error dikirim ke
  stderr.
- Setiap baris CSV berformat: HTF `YYYY-MM-DD,O,H,L,C,V` dan LTF
  `YYYY-MM-DD HH:MM,O,H,L,C,V` (tanpa header, tanpa kolom indeks; kolom
  `volume` memakai `fmt_volume` = `str(Decimal)` dari nilai kline apa adanya).
  Urutan kolom didokumentasikan lewat baris legenda di prosa
  (`Format kolom: tanggal,open,high,low,close,volume`), bukan baris header.
- Prompt juga memuat **tabel sequence swing** (oldest→newest, maksimum
  `swings_table_rows = 12` baris) dengan label HH/HL/LH/LL/EQH/EQL, serta
  **tabel FVG mekanis** per timeframe di dalam bagian §4 (oldest→newest,
  maksimum `fvg_table_rows = 8` gap) dengan baris
  `<stamp>,<FVG_BULLISH|FVG_BEARISH>,<lower>,<upper>,<filled|unfilled>`
  (atau literal `NONE` bila tidak ada gap).

---

### Examples

Bagian ini berisi contoh siap-tempel (copy-paste) untuk skenario umum. Semua
contoh menggunakan CLI yang sudah terpasang (`smc-prompt`). Bila belum
menginstal paket, ganti `smc-prompt` dengan `python -m smc_prompt`.

#### 1. Menjalankan standar (default) pada BTCUSDT

Menghasilkan prompt dengan 60 candle daily HTF, 100 candle hourly LTF,
`--swing-lookback 5`, referensi jarak `nearest`, dan ATR(14) aktif.

```bash
smc-prompt BTCUSDT
```

Keluaran yang diharapkan:

- Prompt lengkap tertulis ke `output/BTCUSDT-<YYYY-MM-DD-HH-MM-SS-UTC>.md`.
- Pesan `[smc-prompt] Prompt written to output/<file>.md.` dan
  `[smc-prompt] Prompt copied to clipboard.` ke **stderr**.
- stdout kosong (tanpa teks prompt) secara default.
- Exit code `0`.

#### 2. Menjalankan tanpa instalasi (via `python -m`)

Cocok bila Anda hanya ingin menjalankan dari source tree tanpa
`pip install -e .`.

```bash
python -m smc_prompt BTCUSDT
```

Keluaran yang diharapkan sama dengan contoh 1.

#### 3. Simbol kustom, case-insensitive

Simbol dinormalisasi otomatis ke huruf besar (`ethusdt` menjadi `ETHUSDT`).

```bash
smc-prompt ethusdt
```

Keluaran yang diharapkan: prompt untuk `PAIR = ETHUSDT`, exit code `0`.

#### 4. Mengatur jumlah candle HTF dan LTF kustom

Mengambil 60 candle daily closed untuk HTF dan 100 candle hourly closed untuk
LTF (nilai default, ditulis eksplisit). Minimum yang valid adalah `10`.

```bash
smc-prompt ETHUSDT --htf-candles 60 --ltf-candles 100
```

Keluaran yang diharapkan: ukuran tabel candle pada prompt mengikuti angka yang
diminta (`HTF_CANDLE_COUNT` dan `LTF_CANDLE_COUNT`). Bila histori closed yang
tersedia lebih sedikit, tabel otomatis diperkecil dan sebuah peringatan
dikirim ke stderr dengan exit code tetap `0`.

#### 4b. Mengubah interval HTF dan LTF

Default adalah HTF `1d` dan LTF `1h`. Kedua interval dapat diubah lewat
`--htf-interval` / `--ltf-interval`; nilai harus salah satu interval Binance
(`1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1M`). Label pada judul prosa
prompt (`Ringkasan Data HTF (...)`, `Sequence Swing Terdeteksi LTF (...)`, dan
`Data Candle Mentah HTF/LTF (...)`) otomatis mengikuti interval yang dipakai.

```bash
smc-prompt BTCUSDT --htf-interval 4h --ltf-interval 15m
```

Keluaran yang diharapkan: data kline diambil pada interval `4h` (HTF) dan `15m`
(LTF); judul prosa prompt menampilkan label `4H` dan `15m`. Nilai interval di
luar daftar akan gagal dengan exit code `2`.

#### 5. Histori lebih panjang untuk konteks lebih banyak

Meminta 200 candle daily dan 300 candle hourly (fetch internal ditambah
`context_buffer` 50, dibatasi maksimum 1000 candle).

```bash
smc-prompt BTCUSDT --htf-candles 200 --ltf-candles 300
```

Keluaran yang diharapkan: tabel candle lebih panjang pada prompt. Prompt menjadi
lebih besar; perhatikan batas panjang paste pada UI chat tujuan.

#### 6. Mengubah ukuran window fractal swing

Window `N` harus bilangan ganjil `>= 3`. Nilai lebih besar menghasilkan swing
yang lebih sedikit namun lebih signifikan.

```bash
smc-prompt SOLUSDT --swing-lookback 7
```

Keluaran yang diharapkan: swing high/low terdeteksi dapat berbeda dari `N = 5`.
Argumen genap atau `< 3` akan gagal dengan exit code `2`.

#### 7. Memilih referensi swing `most-recent`

Melaporkan swing high/low **terbaru berdasarkan timestamp**, bukan yang terdekat
secara harga.

```bash
smc-prompt SOLUSDT --distance-reference most-recent
```

Keluaran yang diharapkan: baris "Swing high/low terdeteksi" pada prompt memakai
swing paling anyar. Nilai selain `nearest`/`most-recent` akan ditolak oleh
`click.Choice` (exit code `2`).

#### 8. Menghilangkan baris ATR (opsional)

ATR(14) aktif secara default. Gunakan `--no-atr` untuk menyembunyikan baris ATR
dari prompt (perhitungan ATR internal tetap digunakan untuk filter swing).

```bash
smc-prompt BTCUSDT --no-atr
```

Keluaran yang diharapkan: prompt tanpa baris `- ATR(14): ... (opsional)` pada
bagian HTF maupun LTF.

#### 9. Memilih host / data source alternatif

Meng-override host REST Binance. Host ini dicoba lebih dulu, lalu fallback ke
host default bila terblokir.

```bash
smc-prompt BTCUSDT --base-url https://data-api.binance.vision
```

Keluaran yang diharapkan: permintaan dilayani oleh host yang diberikan. Bila
host gagal (connection error / HTTP 451 / HTTP 403), CLI berpindah ke host
fallback berikutnya.

#### 10. Menentukan direktori output untuk berkas `.md`

Mengarahkan berkas prompt `.md` ke direktori kustom. Direktori dibuat
otomatis bila belum ada.

```bash
smc-prompt BTCUSDT --output-dir ./hasil
```

Keluaran yang diharapkan:

- Berkas prompt selalu ditulis ke
  `./hasil/BTCUSDT-<YYYY-MM-DD-HH-MM-SS-UTC>.md`.
- Bila clipboard tersedia: prompt juga tersalin, pesan
  `[smc-prompt] Prompt copied to clipboard.` pada stderr.
- Bila clipboard tidak tersedia (headless): hanya peringatan yang dikirim ke
  stderr; berkas prompt tetap ada dan exit code tetap `0`.

#### 11. Mode debug untuk stack trace

Menampilkan stack trace lengkap saat terjadi error, berguna untuk pelaporan bug.

```bash
smc-prompt BTCUSDT --debug
```

Keluaran yang diharapkan: pada error, stack trace dicetak ke stderr selain pesan
error ringkas.

#### 12. Menggabungkan beberapa opsi

Contoh realistis: ETHUSDT, referensi swing terbaru, tanpa ATR, output ke folder
kustom, dan mode debug.

```bash
smc-prompt ETHUSDT --htf-candles 120 --ltf-candles 200 \
  --swing-lookback 5 --distance-reference most-recent \
  --no-atr --output-dir ./output --debug
```

Keluaran yang diharapkan: satu prompt lengkap sesuai seluruh opsi, exit code
`0`.

#### 13. Menampilkan bantuan dan versi

```bash
smc-prompt --help
smc-prompt -h
smc-prompt --version
```

Keluaran yang diharapkan:

- `--help` / `-h`: ringkasan usage, argumen, dan seluruh opsi beserta default.
- `--version`: menampilkan `smc-prompt, version 0.1.0` (versi dari
  [`smc_prompt/__init__.py`](smc_prompt/__init__.py)).

#### 13b. Dry run — validasi konfigurasi tanpa fetch / tulis berkas (Phase 5)

Berguna untuk CI / pre-flight: memvalidasi konfigurasi + simbol lalu mencetak
pengaturan yang diresolusi ke **stderr**. Tidak ada klines yang diambil, tidak
ada berkas yang ditulis.

```bash
smc-prompt BTCUSDT --dry-run
smc-prompt BTCUSDT --dry-run --htf-interval 4h --ltf-interval 15m
```

Keluaran yang diharapkan (stderr):

```text
[smc-prompt] DRY RUN — no file written, no klines fetched.
[smc-prompt] symbol=BTCUSDT
[smc-prompt] data source=Binance network
[smc-prompt] htf_interval=1d (Daily) ltf_interval=1h (1H)
[smc-prompt] htf_candles=60 ltf_candles=100 swing_lookback=5
[smc-prompt] distance_reference=nearest include_atr=True
[smc-prompt] htf_fetch_limit=110 ltf_fetch_limit=150
[smc-prompt] output_dir=output stdout=False
[smc-prompt] volume_mean_period=20 volume_spike_mult=1.5
[smc-prompt] prompt_bytes_warn=120000 max_prompt_bytes=None
```

Exit code `0`. Tidak ada berkas `.md` yang dibuat.

#### 13c. Batas keras ukuran prompt (Phase 5)

Membatalkan (exit `2`) bila prompt hasil render melebihi jumlah byte yang
ditentukan, **sebelum** berkas ditulis. Berguna untuk menjaga prompt tetap di
bawah batas paste UI chat tujuan.

```bash
smc-prompt BTCUSDT --max-prompt-bytes 60000
```

Keluaran yang diharapkan bila terlampaui:

```text
[smc-prompt] ERROR: Rendered prompt is <n> bytes, exceeding --max-prompt-bytes (60000). Reduce --htf-candles/--ltf-candles or raise the limit.
```

Tanpa `--max-prompt-bytes`, hanya ambang lunak `PROMPT_BYTES_WARN`
(default `120000` byte) yang memunculkan `[smc-prompt] WARN:` berisi jumlah byte
dan perkiraan token; exit code tetap `0`.

#### 13d. Mode Post-Trade Review — ekspor data candle & jurnal tanpa prompt

Digunakan setelah posisi di-entry untuk mengevaluasi jalannya trade (*post-review* / jurnal). Mode ini **hanya mengekspor data candle mentah (OHLCV)** dan **ringkasan pergerakan harga sesi** ke berkas `.md`, tanpa memuat teks instruksi analisis SMC pre-trade.

```bash
# Default: 30 candle terakhir pada interval 1h
smc-prompt BTCUSDT --candles-only

# Kustomisasi interval dan jumlah candle
smc-prompt BTCUSDT --candles-only --review-interval 15m --review-candles 50

# Alias --review juga didukung
smc-prompt ETHUSDT --review --review-interval 4h --review-candles 20
```

Berkas ditulis ke `<output_dir>/<SYMBOL>-REVIEW-<TIMESTAMP>.md` dan disalin ke clipboard. Berkas berisi:
- Metadata & rentang waktu candle.
- Ringkasan statistik harga (Open pertama, Close terakhir, Net change %, Highest high, Lowest low, Total range).
- Formulir checklist jurnal trading (Arah Posisi, Entry, SL, TP, Outcome, Evaluasi).
- Tabel CSV candle mentah siap pakai.

#### 13e. Mode Validasi Setup & Deteksi Front-Run (--validate-setup)

Digunakan saat Anda telah memiliki rencana setup (misal limit order di Order Block atau FVG), tetapi ingin mengecek apakah harga sudah sempat mendekati entry lalu berbalik dan melaju ke arah target (*front-runned*), atau bahkan target TP/DOL sudah tercapai duluan sebelum entry terisi (*invalidated*).

```bash
# Validasi Long Setup (arah posisi otomatis disimpulkan long karena TP > Entry)
smc-prompt BTCUSDT --validate-setup --entry 60000 --tp 62500 --sl 59000

# Validasi Short Setup dengan kustomisasi interval dan jumlah candle
smc-prompt ETHUSDT --validate-setup --entry 2500 --tp 2300 --sl 2580 --validate-interval 15m --validate-candles 60

# Alias --check-setup juga didukung
smc-prompt SOLUSDT --check-setup --entry 140 --tp 155
```

Berkas ditulis ke `<output_dir>/<SYMBOL>-VALIDATION-<TIMESTAMP>.md` dan disalin ke clipboard. Prompt ini memuat:
- **Fakta Kuantitatif:** Titik pendekatan terdekat (*closest approach*), selisih jarak ke entry (dalam harga dan normalisasi ATR), status mitigasi entry, status DOL, serta **Rasio Jelajah Target (*Target Travel Ratio*)**.
- **Indikasi Status Mekanis:**
  - `FRESH`: Harga belum terisi dan belum menempuh >=60% ke target TP. Setup masih segar.
  - `FRONT_RUNNED`: Harga berbalik arah sebelum menyentuh entry dan telah menempuh >=60% menuju target TP. Risiko *chasing* tinggi.
  - `DOL_REACHED`: Target TP/DOL telah tersapu sebelum level entry terjemput. Setup gugur (*invalidated*).
  - `TRIGGERED`: Level entry sudah tersentuh/terlewati (posisi sudah aktif).
  - `STOPPED_OUT`: Level SL sudah tertembus.
- **Tabel OHLCV CSV Mentah:** Riwayat pergerakan candle selama sequence pendekatan harga.
- **Panduan Evaluasi Risiko LLM:** Format instruksi terstruktur bagi model AI untuk menilai toleransi spread, pelemahan struktur retracement, dan keputusan limit order (Pertahankan / Batalkan / Tunggu Re-entry).

#### 14. Contoh kegagalan yang umum (beserta exit code)

Simbol tidak terdaftar di Binance Spot (exit code `3`):

```bash
smc-prompt NOTACOIN
```

Keluaran yang diharapkan:
`[smc-prompt] ERROR: Symbol 'NOTACOIN' is not listed on Binance Spot. Check the spelling (e.g. BTCUSDT).`

Argumen tidak valid, mis. `--swing-lookback` genap (exit code `2`):

```bash
smc-prompt BTCUSDT --swing-lookback 4
```

Keluaran yang diharapkan:
`[smc-prompt] ERROR: --swing-lookback must be an odd integer >= 3.`

Jumlah candle di bawah minimum (exit code `2`):

```bash
smc-prompt BTCUSDT --htf-candles 5
```

Keluaran yang diharapkan:
`[smc-prompt] ERROR: --htf-candles must be an integer >= 10.`

Interval tidak valid, mis. `--htf-interval 2H` (exit code `2`):

```bash
smc-prompt BTCUSDT --htf-interval 2H
```

Keluaran yang diharapkan:
`[smc-prompt] ERROR: --htf-interval must be one of: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M.`

---

## Provider Data: Binance, Twelve Data, dan OANDA (Phase 6)

### Kenapa XAUUSD tidak bisa diambil dari Binance

Binance Spot **tidak melisting** instrumen fiat/forex/logam. `GET
/api/v3/exchangeInfo?symbol=XAUUSD` mengembalikan `symbols: []` dan `GET
/api/v3/klines?symbol=XAUUSD` mengembalikan HTTP 400, sehingga keduanya berujung
`SymbolNotFoundError` (exit `3`). Ini masalah **instrumen**, bukan host — jadi
`--base-url` tidak bisa menolong.

**Jangan memakai `XAUTUSDT`/`PAXGUSDT` sebagai pengganti.** Itu token emas, bukan
spot gold:

- **Premium/diskon persisten** terhadap XAU/USD spot — inilah selisih "beberapa
  point" yang terlihat.
- **Mikrostruktur crypto** (orderbook tipis, wick dari trade token besar, drift
  peg USDT/USD) → memunculkan **swing dan FVG hantu** yang tidak ada di pasar gold.
- **Sesi berbeda** — gold spot tutup akhir pekan, Binance 24/7; boundary candle
  `1d` pun bergeser (00:00 UTC vs 21:00/22:00 UTC), sehingga timestamp swing dan
  ambang EQH/EQL ikut bergeser.

### Provider yang tersedia

| Provider | `--provider` | Instrumen | 4h native | Volume | Kredensial |
|---|---|---|---|---|---|
| Binance Spot | `binance` (default) | crypto | ya | ya | tidak perlu |
| Twelve Data | `twelvedata` | FX & logam (`XAU/USD`) | ya | tidak (0) | API key gratis |
| OANDA v20 | `oanda` | FX & logam (`XAU_USD`) | ya | tick count → dinolkan | token (akun practice gratis) |

Keduanya menyajikan `4h` **native**, jadi trio default `1d` / `4h` / `1h` dipetakan
1:1 dan **tidak ada agregasi** yang diperlukan.

### Contoh: XAUUSD via Twelve Data

Daftar API key gratis di <https://twelvedata.com>, lalu:

```bash
# Lewat flag
smc-prompt XAUUSD --provider twelvedata --twelvedata-key "$TWELVEDATA_API_KEY"

# Atau lewat environment variable (lebih nyaman untuk pemakaian rutin)
export TWELVEDATA_API_KEY=xxxxx            # Windows: set TWELVEDATA_API_KEY=xxxxx
smc-prompt XAUUSD --provider twelvedata
```

### Contoh: XAUUSD via OANDA

Buat akun **practice** gratis, ambil token dari portal OANDA, lalu:

```bash
export OANDA_API_TOKEN=xxxxx               # token environment practice
smc-prompt XAUUSD --provider oanda --oanda-env practice

# OANDA live (butuh akun terdanai)
smc-prompt XAUUSD --provider oanda --oanda-env live --oanda-token "$LIVE_TOKEN"
```

### Pemetaan simbol & interval

| Kanonik | Twelve Data | OANDA |
|---|---|---|
| `XAUUSD` | `XAU/USD` | `XAU_USD` |
| `EURUSD` | `EUR/USD` (heuristik FX 6 huruf) | `EUR_USD` |
| `1d` | `1day` | `D` |
| `4h` | `4h` | `H4` |
| `1h` | `1h` | `H1` |

Interval yang tidak didukung provider ditolak dengan `ConfigError` (exit `2`).

### Perbedaan perilaku yang perlu diketahui

- **Fakta volume dinonaktifkan** untuk Twelve Data dan OANDA (`volume_available=False`),
  sehingga baris volume dirender `n/a` / `spike: unknown`. Alasannya: Twelve Data
  melaporkan `0` untuk logam, sementara "volume" OANDA adalah **hitungan tick**,
  bukan volume transaksi — menampilkannya di kolom `volume` akan salah label
  sebagai `[FAKTA]`. Penonaktifan ini juga mematikan heuristik **zero-volume
  "delisted"**; tanpa itu setiap run gold akan memunculkan peringatan palsu
  "may be delisted or halted".
- **Presisi harga** diambil dari `PRICE_FILTER.tickSize` sintetis: Twelve Data
  memakai konvensi 2 dp logam, OANDA memakai `displayPrecision` venue (biasanya
  3 dp untuk `XAU_USD`).
- **Waktu server** — hanya Binance yang punya endpoint jam bursa; provider FX
  memakai jam host untuk keputusan candle closed dan `GENERATED_AT_UTC`. Khusus
  OANDA, flag `complete` dari venue dipakai langsung sebagai penentu candle
  tertutup (otoritatif, bukan perbandingan jam).
- **Boundary candle harian** — gold spot menutup pada 17:00 ET, bukan 00:00 UTC,
  sehingga timestamp swing harian dan ambang EQH/EQL bergeser relatif terhadap
  candle crypto. Ini disengaja: data yang benar untuk instrumen yang benar.
- **Baris provenance prompt** kini mengikuti provider terpilih, mis.
  `live market data API (Twelve Data)` — template tetap provider-agnostic, dan
  output Binance **byte-identik** dengan sebelumnya.

---

## Mode Offline (Data Lokal CSV)

Mulai Phase 4, `smc-prompt` dapat dijalankan **tanpa akses jaringan sama
sekali** dengan membaca candle OHLCV dari berkas CSV lokal. Ini berguna untuk
meregenerasi dan meninjau perubahan template secara **byte-for-byte** tanpa
candle live. **Jalur jaringan tetap menjadi default** — mode offline hanya aktif
bila flag `--input-csv` diberikan.

### Schema CSV

Satu berkas per timeframe, **baris header wajib**:

```
open_time,open,high,low,close,volume[,close_time]
```

| Kolom | Wajib | Format |
|---|---|---|
| `open_time` | ya | ISO-8601 UTC (`2026-07-15T00:00:00Z` / `... +00:00` / `2026-07-15 00:00` / `2026-07-15`); nilai bilangan bulat murni diartikan sebagai **epoch milidetik** |
| `open` | ya | string desimal (`Decimal`) |
| `high` | ya | string desimal |
| `low` | ya | string desimal |
| `close` | ya | string desimal |
| `volume` | ya | string desimal (dirender via `fmt_volume`, byte-stable) |
| `close_time` | tidak | aturan timestamp sama dengan `open_time`; bila tidak ada, diturunkan sebagai `open_time + delta`, dengan `delta` = selang positif pertama antar `open_time` berurutan |

Catatan:
- Nama kolom **case-insensitive**; kolom tambahan diabaikan; baris diurutkan
  secara kronologis sebelum analisis.
- Setiap baris dianggap **sudah tertutup (closed)** — snapshot offline memang
  histori closed — sehingga render offline deterministik dan tidak bergantung
  pada jam host.
- `GENERATED_AT_UTC` diturunkan sebagai `max(close_time) + 1 detik` dari data,
  jadi **nama berkas output dan seluruh isi prompt adalah fungsi murni dari
  berkas input**.
- Harga saat ini (current price) offline = **close candle LTF closed terakhir**.
- Mode offline memerlukan `--htf-interval`, `--mtf-interval`, dan `--ltf-interval`
  yang **berbeda** (satu CSV memetakan tepat ke satu timeframe).

### Contoh

Memakai satu berkas untuk **ketiga** timeframe:

```bash
smc-prompt BTCUSDT --input-csv candles.csv
```

Memakai berkas terpisah untuk HTF, MTF, dan LTF:

```bash
smc-prompt BTCUSDT --input-csv candles_1d.csv \
  --htf-file candles_1d.csv --mtf-file candles_4h.csv --ltf-file candles_1h.csv
```

Keluaran yang diharapkan: prompt lengkap tertulis ke
`output/BTCUSDT-<YYYY-MM-DD-HH-MM-SS-UTC>.md` dengan nilai `GENERATED_AT_UTC`
yang diturunkan dari CSV (deterministik), **tanpa** panggilan jaringan apa pun.

`--htf-file` / `--mtf-file` / `--ltf-file` **wajib** disertai `--input-csv`;
memberikannya tanpa `--input-csv` akan gagal dengan exit code `2`.

---

## Konfigurasi

Secara default `smc-prompt` **tidak memerlukan variabel lingkungan apa pun** —
semua nilai tuning adalah konstanta di
[`smc_prompt/config.py`](smc_prompt/config.py) yang dapat diubah di level kode
(API Python). Pengecualiannya hanya **kredensial provider** FX/logam (Phase 6),
yang bersifat rahasia dan karena itu dibaca dari lingkungan:

| Variabel | Dipakai oleh | Wajib? |
|---|---|---|
| `TWELVEDATA_API_KEY` | `--provider twelvedata` | ya, untuk provider ini |
| `OANDA_API_TOKEN` | `--provider oanda` | ya, untuk provider ini |
| `OANDA_ACCOUNT_ID` | `--provider oanda` | tidak (ditemukan otomatis) |

Binance (provider default) tidak memerlukan kredensial sama sekali.

### Kredensial via `.env` (opsional, direkomendasikan)

Alih-alih meng-`export` key di setiap shell baru, taruh kredensial di berkas
`.env`. CLI memuatnya otomatis saat dijalankan.

**Langkah setup:**

```bash
# 1. Salin templat
copy .env.example .env          # Windows cmd
# cp .env.example .env          # macOS / Linux
# Copy-Item .env.example .env   # Windows PowerShell

# 2. Isi nilainya (buka .env di editor)
TWELVEDATA_API_KEY=key-anda-di-sini

# 3. Jalankan seperti biasa
smc-prompt XAUUSD --provider twelvedata
```

**Urutan prioritas (yang lebih atas menang):**

```
flag CLI  >  environment shell  >  file .env
```

Artinya: variabel yang sudah Anda `export`/`set` di shell **tidak akan ditimpa**
oleh isi `.env`, dan flag `--twelvedata-key` tetap yang paling kuat. Jadi `.env`
aman dipakai sebagai fallback tanpa mengganggu override sesekali.

**Flag terkait:**

| Flag | Kegunaan |
|---|---|
| `--env-file PATH` | Memakai berkas `env` lain, bukan `.env` di direktori kerja. |
| `--no-dotenv` | Mematikan pemuatan `.env` sama sekali (cocok untuk CI/debugging). |

**Catatan penting:**

- `.env` **tidak akan ter-commit** (lihat [`.gitignore`](.gitignore)); templat
  [`.env.example`](.env.example) justru memang di-commit — jangan pernah menaruh key asli di sana.
- Pemuatan `.env` hanya terjadi di entry point CLI, **bukan** di jalur API Python
  ([`smc_prompt/cli.py`](smc_prompt/cli.py) `main()`, bukan `run()`). Ini menjaga
  test suite tetap hermetik dan bebas dari berkas lokal.
- Bila `.env` ada **tetapi** `python-dotenv` tidak terpasang, CLI memunculkan
  `WARN` dan tetap berjalan (exit code `0`) — tidak pernah gagal diam-diam.
- Bila key tetap kosong setelah semua sumber, provider gagal cepat dengan
  `ConfigError` (exit `2`).

Konstanta penting (`smc_prompt/config.py`):

| Konstanta | Default | Keterangan |
|---|---|---|
| `HTF_INTERVAL` | `1d` | Default `--htf-interval` (interval kline Binance untuk HTF). |
| `LTF_INTERVAL` | `1h` | Default `--ltf-interval` (interval kline Binance untuk LTF). |
| `TIME_PATH` | `/api/v3/time` | Endpoint waktu server Binance untuk keputusan candle closed dan `GENERATED_AT_UTC`. |
| `EXCHANGE_INFO_PATH` | `/api/v3/exchangeInfo` | Sumber `PRICE_FILTER.tickSize` (presisi harga) dan `status` simbol. |
| `MAX_PRICE_DECIMALS` | `8` | Batas atas desimal harga dari tickSize. |
| `BINANCE_INTERVALS` | `(1m,3m,5m,15m,30m,1h,2h,4h,6h,8h,12h,1d,3d,1w,1M)` | Himpunan interval kline Binance yang valid (dipakai untuk validasi `--htf-interval`/`--ltf-interval`). |
| `INTERVAL_LABELS` | `{1d: Daily, 1h: 1H, 4h: 4H, ...}` | Pemetaan interval → label yang dirender pada judul prosa prompt. |
| `DEFAULT_BASE_URLS` | `(https://api.binance.com, https://data-api.binance.vision)` | Daftar host fallback yang dapat di-override. |
| `DEFAULT_HTF_CANDLES` | `60` | Default `--htf-candles`. |
| `DEFAULT_LTF_CANDLES` | `100` | Default `--ltf-candles`. |
| `DEFAULT_SWING_LOOKBACK` | `5` | Default `--swing-lookback`. |
| `DEFAULT_SWING_MERGE_ATR_MULT` | `0.5` | Pemisahan minimum antar swing sejenis (`× ATR(14)`); lebih dekat akan digabung. |
| `DEFAULT_ATR_PERIOD` | `14` | Lookback ATR. |
| `DEFAULT_STRUCTURE_MIN_SWINGS` | `4` | Minimum swing untuk mencoba klasifikasi. |
| `DEFAULT_STRUCTURE_LAST_SWINGS` | `6` | Jumlah swing terakhir untuk klasifikasi. |
| `DEFAULT_SWINGS_TABLE_ROWS` | `12` | Jumlah baris tabel sequence swing yang dirender (swing terakhir). |
| `DEFAULT_EQUAL_LEVELS_ATR_MULT` | `0.1` | Toleransi equal highs/lows (`× ATR(14)`) dan penurunan label EQH/EQL. |
| `DEFAULT_FVG_ATR_MULT` | `0.1` | Ukuran minimum Fair Value Gap (`× ATR(14)`); gap lebih kecil dianggap sub-noise dan dibuang. |
| `DEFAULT_FVG_TABLE_ROWS` | `8` | Jumlah FVG terbaru yang dirender per tabel timeframe (menjadi `fvg_table_rows`). |
| `DEFAULT_CONTEXT_BUFFER` | `50` | Candle ekstra yang di-fetch agar swing di tepi kiri tabel tetap terdeteksi. |
| `FETCH_LIMIT_MAX` | `1000` | Batas keras klines Binance. |
| `DEFAULT_REQUEST_TIMEOUT` | `10.0` | Timeout per request (detik). |
| `DEFAULT_RETRY_MAX` | `3` | Jumlah percobaan ulang. |
| `DEFAULT_OUTPUT_DIR` | `output` | Direktori berkas prompt `.md`. |
| `DEFAULT_DELISTED_ZERO_VOLUME_STREAK` | `3` | Ambang peringatan zero-volume. |
| `DEFAULT_VOLUME_MEAN_PERIOD` | `20` | Lookback rata-rata volume untuk volume relatif (`last / mean(N)`). |
| `DEFAULT_VOLUME_SPIKE_MULT` | `1.5` | Candle ditandai spike volume bila volume relatif `≥` nilai ini. |
| `PROMPT_BYTES_WARN` | `120000` | Ambang peringatan lunak ukuran prompt pasca-render (byte); `None` menonaktifkan. |
| `PROMPT_BYTES_PER_TOKEN` | `4` | Pembagi untuk memperkirakan jumlah token pada peringatan ukuran. |
| `MIN_CANDLES` | `10` | Minimum candle yang diminta. |
| `MIN_SWING_LOOKBACK` | `3` | Minimum window fractal. |

Turunan: `htf_fetch_limit = min(htf_candles + context_buffer, fetch_limit_max)`,
sama untuk LTF.

## Struktur Proyek

```
smc_prompt/
├── __init__.py            # konstanta versi paket
├── __main__.py            # memungkinkan `python -m smc_prompt`
├── cli.py                 # entrypoint Click + orkestrasi (satu-satunya penulis stdout/stderr)
├── config.py              # default imutabel, string interval, aturan format, PriceFormat (tickSize)
├── models.py              # dataclass / kontrak payload bertipe
├── errors.py              # hierarki exception (memetakan exit code) + warning non-fatal
├── data_fetcher.py        # klien REST Binance (klines/ticker/exchangeInfo/time) + retry/backoff + failover
├── provider_base.py       # scaffolding provider bersama: HTTP retry/failover, timestamp, agregasi, price-band
├── twelvedata_source.py   # data source Twelve Data (XAU/USD, 4h native)
├── oanda_source.py        # data source OANDA v20 (XAU_USD, H4 native, flag `complete`)
├── structure_analyzer.py  # deteksi swing, klasifikasi, jarak, ATR
├── template_renderer.py   # pembangun placeholder + render Jinja2
├── output.py              # tulis berkas .md + salin clipboard (best-effort)
└── templates/
    └── prompt_template.j2 # template prompt (byte-frozen)

docs/
└── DESIGN_SPEC.md         # spesifikasi desain beku

pyproject.toml             # metadata paket, dependensi, entry point, extra [dev]
requirements.txt           # daftar dependensi runtime
README.md                  # dokumen ini
```

### Tests

Suite uji berbasis **pytest**, **tanpa jaringan** (fetcher tidak pernah dipanggil
tanpa mock; jalur data memakai sumber CSV offline). Suite mencakup:
`detect_swings` (equal highs + tabrakan ATR), `classify_structure`
(HH/HL, LH/LL, mixed/ranging, equal-levels, kasus < 2 highs), `fmt_distance`
(sign/magnitudo), `prepare_series` + `InsufficientDataError`, `enforce_alternation`,
`detect_equal_levels`, `detect_fvg` (bounds/birth_time/filled/sub-noise), derivasi
`tickSize → desimal`, skema CSV + parity data source, dan satu **golden test**
yang memakukan hash prompt byte-for-byte.

```bash
# Pasang extra dev (pytest) lalu jalankan suite
pip install -e ".[dev]"
pytest
```

Fixture deterministik ada di `tests/fixtures/` (`htf_daily.csv`,
`ltf_hourly.csv`). Regenerasi dengan
`python tests/fixtures/generate_fixtures.py` lalu tinjau diff-nya. Hash golden
yang dipatok adalah
`f5a47d8fac567316102faf9a8c19e0a3130d65bb4272b74b0b71f0ebd8bd30c7`
(`18684` byte). Bila template atau placeholder berubah, regenerasi berkas
lalu perbarui `GOLDEN_SHA256`/`GOLDEN_BYTES` di
[`tests/conftest.py`](tests/conftest.py) setelah meninjau diff byte-for-byte.

## Klasifikasi Struktur

`structure_class` adalah perbandingan mekanis murni dari swing terakhir:

- **Bullish** — dua swing high terakhir membentuk Higher High **dan** dua swing
  low terakhir membentuk Higher Low.
- **Bearish** — dua swing high terakhir membentuk Lower High **dan** dua swing
  low terakhir membentuk Lower Low.
- **Equal Highs/Lows** — dua swing high terakhir **dan** dua swing low terakhir
  masing-masing berada dalam toleransi `0.1 × ATR(14)` (liquidity pool). Ini
  fakta tersendiri, sengaja **tidak** dilebur ke `Ranging/Mixed`.
- **Ranging/Mixed** — selain di atas, atau kurang dari dua high / dua low.

## Deteksi Swing

N-bar Williams Fractal (default `N = 5`) dengan post-filter deterministik: dua
swing sejenis berurutan yang berjarak kurang dari `0.5 × ATR(14)` digabung,
menyisakan yang lebih ekstrem. Sebuah post-pass O(n) (`enforce_alternation`)
lalu memampatkan setiap rentetan swing sejenis menjadi satu titik paling ekstrem
(max untuk HIGH, min untuk LOW) sehingga dihasilkan skeleton `H/L/H/L` yang
benar-benar bergantian. Tanpa random, tanpa rekursi, tanpa lookahead melebihi
window.

Candle terakhir yang **belum tertutup (half-open) dikecualikan** dari seluruh
perhitungan swing, ATR, klasifikasi, dan tabel. Candle tersebut hanya dipakai
sebagai fallback harga saat ini bila endpoint ticker tidak tersedia (endpoint
ticker adalah sumber utama).

## Fair Value Gap (FVG) Mekanis

FVG dihitung secara **murni mekanis** dari OHLC (tanpa interpretasi), sehingga
masuk lapisan `[FAKTA]`. `detect_fvg(candles, min_gap_atr_mult, *, atr_value)`
memindai seri closed satu kali:

- **Bullish FVG** pada candle `i` bila `low[i] > high[i-2]`; rentang
  `[lower, upper] = [high[i-2], low[i]]`.
- **Bearish FVG** pada candle `i` bila `high[i] < low[i-2]`; rentang
  `[lower, upper] = [high[i], low[i-2]]`.

Setiap FVG menyimpan rentang harga (`lower`/`upper`), arah (`bullish`/`bearish`),
**timestamp lahir** (open time candle ke-3), dan flag `filled`. **Filter
sub-noise:** gap yang lebih sempit dari `fvg_atr_mult × ATR(14)` (default `0.1`)
dibuang.

**Status fill.** Bullish FVG `filled` bila ada candle **closed** setelahnya dengan
`low <= lower`; bearish FVG `filled` bila ada candle setelahnya dengan
`high >= upper`. Mekanis, hanya seri closed, tanpa lookahead melebihi window
3 candle.

`fvg_table_rows` (default `8`) FVG terbaru per timeframe dirender sebagai blok
berpagar di dalam bagian §4 template; literal `NONE` dipakai bila tidak ada gap.
Baris: `<stamp>,<FVG_BULLISH|FVG_BEARISH>,<lower>,<upper>,<filled|unfilled>`.

## Sinyal Turunan ATR & Volume (Phase 5)

Phase 5 menambah fakta **mekanis murni** (lapisan `[FAKTA]`) ke Layer A:

- **Jarak ternormalisasi ATR (#6).** Selain jarak persen/absolut, tiap swing
  kini menyertakan `x{abs_distance / ATR(14):.2f}×ATR` (mis. `x2.85×ATR(14)`).
  Placeholder: `HTF_DIST_TO_HIGH_ATR` / `HTF_DIST_TO_LOW_ATR` (dan ekuivalen
  LTF). **Guard:** bila ATR `None` atau `0`, nilai menjadi literal `n/a`
  (tidak pernah terjadi divide-by-zero).
- **ATR sebagai persentase harga (#6).** Baris ringkas `ATR ≈ 2.93% of price`
  (`{{ATR_PCT_OF_PRICE}}`) dihitung dari `ATR(14) / current_price × 100`,
  `abs`, 2 desimal; jatuh ke `n/a` bila ATR tak tersedia / harga non-positif.
- **Volume relatif & flag spike (#16).** `compute_relative_volume` menghitung
  `last / mean(N)` atas `N = volume_mean_period` (default `20`) candle closed
  terakhir, dikuantisasi 4 desimal untuk byte-stability. Candle ditandai spike
  bila `relative ≥ volume_spike_mult` (default `1.5`). Placeholder:
  `*_VOLUME_RELATIVE` (mis. `x1.05`) dan `*_VOLUME_SPIKE`
  (`yes` / `no` / `unknown`). **Guard:** kurang dari 2 candle → `n/a`; mean nol
  (mis. simbol halt/delisted) → `relative = 0` sehingga tak ada divide-by-zero.

## Guard Ukuran Prompt (Phase 5, #11)

Setelah render, ukuran prompt dihitung dalam **byte UTF-8**:

- Bila melebihi ambang lunak `PROMPT_BYTES_WARN` (default `120000`), sebuah
  `[smc-prompt] WARN: Rendered prompt is <n> bytes (~<t> tokens), above the
  120000-byte warning threshold.` dikirim ke stderr (perkiraan token =
  `bytes / PROMPT_BYTES_PER_TOKEN`, default `4`); exit code tetap `0`.
- Bila flag `--max-prompt-bytes` diberikan dan terlampaui, tool membatalkan
  dengan `ConfigError` (exit code `2`) **sebelum** berkas ditulis.

## Exit Codes

| Kode | Arti |
|---|---|
| 0 | Sukses (peringatan mungkin tetap dikeluarkan). |
| 1 | Error internal tak terduga. |
| 2 | Argumen / konfigurasi tidak valid. |
| 3 | Simbol tidak terdaftar di Binance Spot. |
| 4 | Binance API tidak dapat dijangkau setelah retry. |
| 5 | Histori closed tidak cukup untuk menghitung struktur. |
| 6 | Gagal menulis berkas prompt `.md` output. |

## Troubleshooting

- **`Clipboard unavailable ... Prompt written to <path>`** — lingkungan headless
  atau `xclip`/`xsel` tidak terpasang. Ini **hanya peringatan** (exit code tetap
  `0`); berkas prompt sudah tertulis di `--output-dir`; tidak ada tindakan
  tambahan yang diperlukan.
- **`Symbol '<SYM>' is not listed on Binance Spot` (exit 3)** — periksa ejaan
  simbol; gunakan format seperti `BTCUSDT` (base + quote, tanpa pemisah).
- **`Binance API unreachable ...` (exit 4)** — masalah jaringan atau host
  terblokir (mis. HTTP 451 geo-block). Coba
  `--base-url https://data-api.binance.vision`.
- **`Not enough closed <tf> history ...` (exit 5)** — histori closed tidak
  mencukupi. Gunakan simbol dengan histori cukup, atau (mode offline) berkas CSV
  dengan lebih banyak baris.
- **`Offline CSV ...` (exit 2)** — berkas CSV tidak ditemukan, kolom wajib
  hilang, isi kosong, atau ada timestamp/angka yang tidak valid. Periksa header
  `open_time,open,high,low,close,volume` dan format nilainya (lihat "Mode Offline").
- **`--htf-file / --mtf-file / --ltf-file require --input-csv ...` (exit 2)** —
  flag berkas per-timeframe butuh `--input-csv` agar mode offline aktif secara
  eksplisit.
- **`... must be three DISTINCT intervals ...` (exit 2)** — `--htf-interval`,
  `--mtf-interval`, dan `--ltf-interval` tidak boleh sama; ketiganya harus
  berbeda agar tiap tier memetakan ke seri sendiri.
- **`Rendered prompt is <n> bytes, exceeding --max-prompt-bytes ...` (exit 2)** —
  prompt melebihi batas keras. Turunkan
  `--htf-candles`/`--mtf-candles`/`--ltf-candles` atau naikkan
  `--max-prompt-bytes`. Bila hanya peringatan lunak yang muncul (tanpa
  `--max-prompt-bytes`), prompt tetap ditulis (exit `0`).
- **Argumen ditolak (exit 2)** — pastikan `--swing-lookback` ganjil `>= 3` dan
  `--htf-candles`/`--mtf-candles`/`--ltf-candles` `>= 10`.
- **Karakter non-ASCII pada Windows** — CLI memaksa stream stdout/stderr ke
  UTF-8 secara otomatis; tidak perlu konfigurasi manual.
- **`Symbol <SYM> has exchange status '<status>' (not TRADING)` (peringatan)** —
  simbol terdaftar tetapi pasar sedang tidak trading (mis. `BREAK`). Prompt
  tetap dihasilkan dari data historis; ini hanya peringatan (exit code `0`).
- **`Ticker price rejected ... Using last closed <tf> candle close ...` (peringatan)** —
  harga ticker non-positif atau di luar rentang wajar; tool memakai close candle
  closed terakhir. Hanya peringatan (exit code `0`).
- **`Binance server time unavailable ... falling back to the host clock` (peringatan)** —
  endpoint waktu server tidak dapat dijangkau; tool memakai jam host untuk
  keputusan candle closed dan `GENERATED_AT_UTC`. Hanya peringatan (exit code `0`).

## Catatan dan Batasan Diketahui

- **Peringatan sanity referensi (non-fatal).** Bila `structure_class` Bullish
  tetapi swing low terdekat sudah berada **di atas** harga saat ini (level
  rusak), atau Bearish tetapi swing high terdekat sudah **di bawah** harga,
  `reference_sanity_warnings` mengeluarkan baris `[smc-prompt] WARN:` ke stderr.
  Prompt tetap dirender dengan fakta mentah; exit code tetap `0`.
- **Presisi harga diturunkan dari tick size simbol** (`PRICE_FILTER.tickSize`
  dari `exchangeInfo`): jumlah desimal mengikuti tickSize (mis.
  `0.01000000` → 2 dp, `0.00000100` → 6 dp, `1.00000000` → 0 dp), dibatasi 8 dp.
  Bila tick tidak tersedia/tidak dapat diparse, fallback ke aturan berbasis
  magnitudo: `>= 1000` → 2 desimal, `>= 1` → 4 desimal, `< 1` → 8 desimal.
  Kedua jalur deterministik; untuk sampel BTCUSDT beku keduanya identik.
- **Waktu closure memakai jam server Binance** (`GET /api/v3/time`), bukan jam
  host, sehingga jam host yang miring tidak mengklasifikasi candle terakhir
  secara keliru. Bila server time tidak tersedia, jam host dipakai dengan
  peringatan `[smc-prompt] WARN:` dan exit code tetap `0`.
- **Peringatan status simbol non-fatal.** Bila `status` simbol dari
  `exchangeInfo` bukan `TRADING` (mis. `BREAK`/`HALT`), sebuah
  `[smc-prompt] WARN:` dikirim ke stderr. Prompt tetap dirender dengan fakta
  mentah; exit code tetap `0`.
- **Peringatan harga saat ini (sanity).** Harga ticker yang non-positif atau
  berada di luar rentang `[low, high]` candle closed LTF terakhir ± 1×ATR
  ditolak dan diganti dengan close candle closed terakhir, disertai
  `[smc-prompt] WARN:`. Prompt tetap dirender; exit code tetap `0`.
- **`half = (N-1)/2` bar closed terbaru tidak pernah bisa menjadi swing**,
  sehingga swing yang dilaporkan bisa tertinggal beberapa bar. Ini melekat pada
  metode fractal.
- **Heuristik zero-volume untuk simbol delisted hanyalah peringatan** (exit 0).
  Pasangan bervolume rendah namun tetap tradable dapat memicu peringatan palsu.
- Semua timestamp adalah **UTC**.
- Prompt tidak disanitasi terhadap prompt-injection, tetapi seluruh konten yang
  disuntikkan adalah data numerik/simbol yang dikendalikan oleh alat ini.

---

## Lisensi

Dirilis di bawah **MIT License** (lihat metadata `license = { text = "MIT" }`
pada [`pyproject.toml`](pyproject.toml)).
