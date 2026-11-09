# ADR-0005: Standarisasi Default Timeframe Harian (1d/1h/15m) dan Matriks Trading Style Preset

Perubahan konfigurasi default multi-timeframe dari `4h/1h/15m` ke `1d/1h/15m` (Konservatif / Daily Bias) serta pengenalan fitur Trading Style Preset terstandarisasi (`intraday`, `sniper`, `balanced`, `swing`, dan `scalp`) pada antarmuka CLI dan Wizard Interaktif.

## Status
Accepted

## Konteks
Sebelumnya, `smc-prompt` menggunakan kombinasi default `4h` (HTF), `1h` (MTF), dan `15m` (LTF). Meskipun memiliki rasio fraktal yang seimbang (4x -> 4x), trader institusional dan praktisi SMC/ICT sering kali menghadapi tantangan:
1. **Bias Makro Kurang Kokoh:** Struktur 4H bergerak lincah (6 candle per hari pada kripto), sehingga sering kali pergeseran likuiditas jangka pendek mengaburkan narasi makro utama (*Daily Draw on Liquidity* / DOL, PDH/PDL, dan Daily FVG).
2. **Keterbatasan Fleksibilitas Gaya Trading:** Pengguna yang ingin menganalisis dengan gaya trading berbeda (seperti *Sniper Killzone* 5m untuk SL ketat, *Swing* 1d/4h/1h, atau *Scalp* 1h/15m/5m) harus selalu mengetikkan ketiga flag interval secara manual (`--htf-interval`, `--mtf-interval`, `--ltf-interval`) yang memakan waktu dan rentan kesalahan input.
3. **Ketidakselarasan dengan Panduan SMC:** Dokumen referensi strategi membagi eksekusi multi-timeframe ke dalam hierarki 3-Tier yang tegas (Swing, Intraday, Scalp) dengan karakteristik profil risiko dan toleransi stop loss yang berbeda.

## Keputusan
1. **Pembaruan Default Global ke `1d / 1h / 15m`:**
   - Mengubah default global `HTF_INTERVAL` menjadi `1d`, `MTF_INTERVAL` tetap `1h`, dan `LTF_INTERVAL` tetap `15m` di `config.py`.
   - Mengadopsi profil *Intraday Konservatif* sebagai standar utama: level *Previous Day High/Low* (PDH/PDL) menjadi magnet likuiditas yang lebih stabil, rasio fraktal 1H ke 15m (4x) memfilter noise pasar dengan bersih, dan jendela render 40 candle LTF mencakup ~10 jam (cukup untuk sesi London hingga New York Killzone).
   - Seluruh provider bursa (Binance, Bitunix, MEXC, Gate, Twelve Data, OANDA) terbukti mendukung interval `1d` secara native.
2. **Katalog Trading Style Preset (`TIMEFRAME_PRESETS`):**
   - Mendefinisikan 5 preset resmi:
     * `intraday` (Default): `1d` (HTF) / `1h` (MTF) / `15m` (LTF) — Konservatif, fokus sapuan PDH/PDL.
     * `sniper`: `4h` (HTF) / `1h` (MTF) / `5m` (LTF) — Cepat tanggap momentum, entri presisi dengan SL ketat di sesi aktif.
     * `balanced`: `4h` (HTF) / `1h` (MTF) / `15m` (LTF) — Rasio harmonis 4x fraktal (profil default sebelumnya).
     * `swing`: `1d` (HTF) / `4h` (MTF) / `1h` (LTF) — Holding bertempo multi-hari hingga minggu.
     * `scalp`: `1h` (HTF) / `15m` (MTF) / `5m` (LTF) — Eksekusi cepat hitungan menit/jam pada *session extremes*.
3. **Dukungan Antarmuka Terpadu (CLI & Wizard):**
   - **CLI One-Liner:** Menambahkan flag `--preset [intraday|sniper|balanced|swing|scalp]`. Flag interval individual (`--htf-interval`, dll.) dapat digunakan bersamaan untuk meng-override interval dari preset jika diperlukan.
   - **Interactive Wizard:** Memperbarui Langkah 4 dengan menu pilihan preset bernomor, di mana Opsi 1 (`intraday`) langsung terpilih sebagai default saat menekan tombol Enter, serta menyertakan opsi manual `custom`.
   - **Generator Perintah:** Pada ringkasan akhir wizard, perintah one-liner yang dirakit mencetak flag `--preset <nama>` jika memilih preset non-default, menjaga perintah tetap ringkas.
4. **Keseragaman Candle Window:**
   - Mempertahankan jumlah candle fetch (90/186/192) dan render tail (20/30/40) tetap seragam di seluruh preset demi menjaga ukuran payload LLM (~12-18 KB) dan kestabilan perhitungan indikator ATR(14).

## Konsekuensi
- Output perintah default `smc-prompt <SYMBOL>` kini secara otomatis menghasilkan analisis dengan jangkar bias harian (`1d`).
- Pengguna dapat beralih gaya trading secara instan menggunakan flag satu kata `--preset sniper` atau `--preset swing`.
- Backward compatibility tetap terjaga: pengguna yang menentukan `--htf-interval`, `--mtf-interval`, atau `--ltf-interval` secara eksplisit tetap mendapatkan interval yang diminta.
