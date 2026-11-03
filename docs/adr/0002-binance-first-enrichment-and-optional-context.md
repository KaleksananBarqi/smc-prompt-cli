# Binance-First Auxiliary Context Enrichment & Graceful Degradation

Pengayaan konteks pasar tambahan pada header prompt—meliputi **Konteks Derivatif** (Funding Rate & Open Interest beserta delta 4 jam) serta **Acuan Struktur BTCUSDT** untuk deteksi *SMT Divergence*—diterapkan menggunakan strategi **Binance-First with Graceful Degradation** dan disuntikkan sebagai variabel render opsional (`derivatives_context_line`, `btc_benchmark_line`) di luar kontrak ketat `REQUIRED_PLACEHOLDERS`.

## Status
Accepted

## Konteks
Pipeline utama `smc-prompt` memberlakukan kontrak non-kosong yang ketat (`REQUIRED_PLACEHOLDERS`) terhadap seluruh metrik harga dan struktur candlestick (HTF, MTF, LTF) yang ditarik dari provider aktif dalam *Crypto Failover Chain*. Jika salah satu data inti tersebut gagal dihitung atau kosong, pembuatan prompt dihentikan dengan galat keras agar LLM tidak pernah menerima fakta struktural yang cacat.

Namun, data konteks tambahan seperti Funding Rate, Open Interest (OI), dan perbandingan struktur acuan `BTCUSDT` memiliki karakteristik yang berbeda:
1. **Kedalaman Likuiditas Acuan:** Binance Futures (USDT-M) merupakan pasar derivatif dengan likuiditas global paling dalam, sehingga angka Funding Rate dan Open Interest dari Binance menjadi tolok ukur institusional paling representatif.
2. **Keterbatasan Lintas-Mode & Lintas-Aset:** Data derivatif kripto tidak relevan atau tidak tersedia pada penyedia data FX/logam (`twelvedata`, `oanda`) maupun saat berjalan dalam mode *offline* (`--input-csv`). Selain itu, kegagalan atau pemblokiran endpoint statistik tambahan (seperti `/futures/data/openInterestHist`) tidak boleh menggagalkan pembuatan prompt analisis struktur utama ketika data candlestick OHLCV sudah berhasil diperoleh.

## Keputusan
1. **Strategi Binance-First dengan Fallback ke Provider Aktif:**
   - Untuk simbol kripto pada mode jaringan *live*, sistem selalu mencoba mengambil konteks tambahan dari **Binance Futures** terlebih dahulu sebagai acuan likuiditas global utama.
   - Apabila endpoint Binance tidak dapat dihubungi (misalnya terblokir ISP/HTTP 451/403 atau *timeout*) atau simbol tidak terdaftar di Binance, sistem secara otomatis beralih (*fallback*) menggunakan penyedia kripto aktif yang telah berhasil menarik data OHLCV (`bitunix`, `mexc`, atau `gate`).
2. **Degradasi Anggun (*Graceful Degradation*):**
   - Jika delta OI 4 jam (`/futures/data/openInterestHist`) gagal dimuat atau mengembalikan kurang dari 2 titik data, baris konteks derivatif tetap dirender menggunakan nilai Funding Rate dan Open Interest saat ini tanpa delta.
   - Jika seluruh percobaan pengambilan konteks tambahan gagal, sistem mengembalikan string kosong (`""`) secara senyap tanpa memicu `NetworkError` atau menghentikan eksekusi CLI.
3. **Invariansi Zero-Network pada Mode Offline & Non-Kripto:**
   - Ketika dijalankan dalam mode `--input-csv` (*offline*) atau menggunakan provider FX/logam (`twelvedata`, `oanda`), pengambilan konteks derivatif dan benchmark BTC dilewati sepenuhnya (0 panggilan jaringan).
4. **Variabel Render Opsional pada Template:**
   - Baris pengayaan diteruskan ke `template_renderer.render()` sebagai variabel render opsional (`DERIVATIVES_CONTEXT_LINE` / `derivatives_context_line` dan `BTC_BENCHMARK_LINE` / `btc_benchmark_line`) yang dibungkus blok kondisional Jinja2 `{% if ... %}` dengan `trim_blocks=True`.
   - Saat bernilai kosong (`""`), baris tersebut dihilangkan secara bersih tanpa menyisakan baris kosong (*blank-line artifacts*), sehingga output *golden render* maupun mode *offline* tetap identik byte-demi-byte.

## Konsekuensi
- Analis AI memperoleh konteks posisi pasar derivatif (Funding Rate & OI) dari sumber likuiditas terdalam (Binance) atau exchange alternatif yang aktif tanpa mengorbankan keandalan CLI di jaringan yang terblokir.
- Kegagalan endpoint statistik sekunder tidak pernah membatalkan pembuatan prompt utama.
- Pengujian hermetik (*offline CSV*, *golden render*, dan *provider unit tests*) tetap bersih, deterministik, dan bebas dari panggilan jaringan tak terduga.
