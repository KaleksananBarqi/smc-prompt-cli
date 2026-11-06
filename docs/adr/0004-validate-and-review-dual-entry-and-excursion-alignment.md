# ADR-0004: Penyelarasan Mode Validasi Setup dan Post-Trade Review dengan Fitur SMC v2

Penyelarasan mode validasi setup (`--validate-setup`) dan review pasca-trade (`--review` / `--candles-only`) dengan fitur arsitektur SMC v2: skema Dual-Entry (Entry 1 Aggressive, Entry 2 Moderate, Blended Entry, TP1, TP2), status order *Partial Fill*, perhitungan mekanis ekskursi harga (MFE/MAE dan *First-Touch Sequence*), penyertaan opsional ringkasan struktur Layer-A, perbaikan durasi interval compact timestamp, dan pengayaan konteks derivatif/benchmark BTC.

## Status
Accepted

## Konteks
Setelah implementasi SMC v2 dan ADR-0003 (*Anti-Front-Run Dual-Entry Schema and POI Proximity Filtering*), prompt utama `smc-prompt` telah mengadopsi skema dua tingkat harga (*Entry 1 Aggressive* dan *Entry 2 Moderate* dengan acuan *Blended Entry*), target bertingkat (*TP1 Nearest DOL* dan *TP2 Main DOL*), *Dealing Range* & *Equilibrium (EQ)*, sesi *Killzone* UTC, pengayaan derivatif, dan format *compact timestamp* berbasis durasi interval (`< 1d` vs `>= 1d`).

Namun, mode evaluasi hilir—yaitu `--validate-setup` dan `--review`—masih beroperasi dengan asumsi model tunggal lama (*single entry* dan *single TP*):
1. **Ketidaksesuaian Skema Entry:** Setup Dual-Entry yang dihasilkan prompt utama tidak dapat divalidasi langsung tanpa memaksa pengguna mengorbankan salah satu level entry.
2. **Ketiadaan Status Partial Fill:** Tidak ada mekanisme untuk menangani kondisi yang sangat umum di mana Entry 1 terisi (40% size) namun Entry 2 masih pending di exchange saat harga mulai bergerak menuju TP1.
3. **Ketiadaan Metrik Ekskursi Mekanis:** Evaluasi pasca-trade pada review mode masih menyerahkan perhitungan *floating profit/loss* ke interpretasi LLM tanpa angka pasti MFE (*Maximum Favorable Excursion*) dan MAE (*Maximum Adverse Excursion*) dalam kelipatan R ($+X\text{R} / -X\text{R}$).
4. **Distorsi Format Timestamp pada Timeframe Multi-Hari:** Pengecekan interval hanya mencakup `interval == "1d"`, mengabaikan timeframe non-harian seperti `3d`, `1w`, dan `1M` sehingga format timestamp dan legend CSV tidak konsisten.
5. **Absennya Konteks Struktur & Derivatif pada Validasi:** Mode validasi tidak menyertakan konteks struktur mekanis (swings, Dealing Range/EQ, FVG `ce`, OB `mt`), memaksa model AI berhalusinasi saat diminta mengevaluasi MSS/CHoCH atau pelemahan struktur.

## Keputusan
1. **Modul Domain Terpadu `trade_plan.py`:**
   - Menyediakan dataclass `TradePlan` dan builder `build_trade_plan()` sebagai *single source of truth* untuk validasi guardrail harga, inferensi arah trading, kalkulasi rasio risiko 1R dan RRR terencana (baik dari *Blended Entry* maupun dari *Entry 1*).
   - Menjamin *backward compatibility* 100%: parameter lama `--entry` dan `--tp` tetap berlaku sebagai alias untuk Entry 1 dan TP1.
2. **Dukungan Status Order `partial` pada Mode Validasi:**
   - Menambahkan status order `partial` (`--partial` / `--status partial`) yang merepresentasikan kondisi fakta mutlak: Entry 1 terisi dan aktif, sedangkan Entry 2 masih *pending limit order*.
   - Menyuntikkan panduan guardrails anti-halusinasi terpisah bagi trader untuk mengevaluasi *trade management* posisi aktif E1 sekaligus menentukan nasib *pending limit* E2 (misal membatalkan order E2 jika TP1 sudah tercapai atau momentum berbalik).
3. **Kalkulasi Mekanis Ekskursi Harga (MFE, MAE, dan First-Touch Sequence):**
   - Mengimplementasikan `compute_excursion()` untuk melacak secara deterministik puncak keuntungan (*MFE*) dan penarikan terdalam (*MAE*) dalam nominal poin dan kelipatan R, urutan kronologis sentuhan pertama (*first-touch*: apakah TP1 tercapai sebelum SL atau sebaliknya), dan sesi *Killzone* saat entry pertama terisi.
4. **Penyertaan Opsional Konteks Struktur Layer-A (`render_structure_context_block`):**
   - Mengintegrasikan hasil analisis struktur mekanis (*Dealing Range*, EQ 50%, sequence swings dengan status penembusan, FVG dengan nilai tengah `ce`, dan OB aktif) ke dalam output validasi dan review jika data candle historis mencukupi ($\ge 25$ candle), dengan flag `--no-structure` untuk menonaktifkannya.
5. **Pengayaan Derivatif dan Benchmark BTC pada Mode Validasi:**
   - Menyertakan baris konteks derivatif (Funding Rate, Open Interest) dan structural benchmark `BTCUSDT` pada dokumen prompt validasi kripto live untuk membantu evaluasi pelemahan momentum dan divergensi SMT.
6. **Perbaikan Format Timestamp:**
   - Menggunakan helper `cfg.is_intraday_interval(interval)` secara konsisten pada renderer validasi dan review sehingga seluruh interval multi-hari (`1d`, `3d`, `1w`, `1M`) menggunakan format tanggal compact `%m-%d` dan header kolom `tanggal(UTC)`.

## Konsekuensi
- Validasi setup dan review pasca-trade kini sepenuhnya selaras dengan skema trading plan SMC v2 dan ADR-0003.
- Jurnal trading review menyajikan angka kuantitatif MFE/MAE nyata berbasis data candle, mencegah halusinasi model AI saat mereview efisiensi eksekusi dan penempatan stop loss.
- Semua 385 unit tests dan skenario CLI tetap kompatibel ke belakang.
