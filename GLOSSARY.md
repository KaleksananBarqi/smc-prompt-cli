# Glossary

### Render Tail
Jumlah candlestick tertutup (*closed candles*) paling akhir yang dicetak ke dalam tabel prompt CSV (default: 20 HTF, 30 MTF, 40 LTF). Berbeda dari *Fetch Window*, Render Tail bertujuan membatasi konsumsi token LLM tanpa mengorbankan akurasi perhitungan indikator teknis.

### Fetch Window
Jumlah candlestick tertutup yang ditarik dari penyedia data (*data provider*) dan diproses oleh mesin analisis struktur (*Layer-A structure analyzer*) (default: 90 HTF, 186 MTF, 192 LTF). Histori penuh ini diperlukan agar perhitungan ATR(14), sekuens fraktal swing, dan deteksi Fair Value Gap tetap stabil dan tidak terdistorsi.

### Unfilled FVG
Fair Value Gap mekanis (pola 3-candle) yang belum pernah tersentuh atau tertembus penuh oleh rentang harga candle-candle berikutnya dalam deret data tertutup.

### Recently Mitigated FVG
Fair Value Gap yang telah tertembus (*filled*), namun penembusannya terjadi baru saja pada $\le 3$ candle terakhir dari deret data tertutup. Informasi ini dipertahankan sebagai konteks mitigasi likuiditas terkini bagi model AI.

### Compact Timestamp
Format penulisan waktu ringkas tanpa prefiks tahun (`MM-DD` untuk timeframe Daily/HTF, `MM-DD HH:MM` untuk timeframe intraday MTF/LTF) pada tabel data prompt. Tahun referensi utama tetap tercatat di header dokumen (`Waktu generate (UTC)`).

### Order Block (OB)
Candlestick berbasis zona penumpukan likuiditas institusional (*base candle*) sebelum terjadinya *rally* atau *drop* eksplosif (*displacement*). Ditetapkan secara deterministik hanya jika diikuti oleh terbentuknya Fair Value Gap (FVG-Linked OB) dengan ukuran gap $\ge 0.1 \times \text{ATR}$.

### Mean Threshold (MT)
Titik tengah (level 50%) dari rentang suatu Order Block (`mt = (low + high) / 2`). Berfungsi sebagai batas reaksi kritis di mana penetrasi harga melebihi MT mengindikasikan melemahnya kekuatan penolakan zona OB.

### Wick Sweep
Penolakan manipulatif 1-candle (*1-Candle Rejection*) di mana *wick* candlestick sempat menembus level Swing High atau Swing Low terdahulu, tetapi *body close* kembali ke dalam rentang level tersebut.

### Reclaim Sweep
Manipulasi likuiditas 2-candle (*2-Candle Stop Hunt*) di mana candlestick pertama sempat *close* tipis di luar level swing, namun candlestick berikutnya langsung *close* kembali ke dalam rentang level swing.

### Break of Structure (BOS)
Penembusan struktural riil (*Structural Break*) di mana harga menembus level swing dan berhasil bertahan dengan *close* di luar level tersebut selama minimal 2 candlestick berturut-turut ($\ge 2$ consecutive closes). Membedakan kelanjutan tren dari manipulasi likuiditas (*sweep*).

### Crypto Failover Chain
Mekanisme pengalihan otomatis berurutan antar-exchange kripto publik (`binance` ➔ `bitunix` ➔ `mexc` ➔ `gate`) saat terjadi kegagalan koneksi jaringan (`NetworkError`), guna menjamin kontinuitas penarikan data pasar tanpa interupsi manual.
_Avoid_: Provider Fallback, Exchange Redundancy, Network Retry.

### Strict Provider Mode
Mode eksekusi di mana pengalihan penyedia data otomatis dinonaktifkan (`--no-fallback`), memaksa perintah gagal seketika jika exchange yang diminta secara eksplisit tidak dapat dihubungi.
_Avoid_: Hard Mode, Fail-Fast Flag, No-Failover.

