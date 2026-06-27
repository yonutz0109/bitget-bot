# Bitget Trading Bot

Bot automat de trading pe Bitget Spot.

## Strategie
- Cumpără când RSI < 30 (oversold)
- Vinde când RSI > 70 SAU profit +5% SAU stop-loss -3%
- Monitorizează: BTC, ETH, XRP
- Verifică piața la fiecare oră

## Deploy pe Render.com
1. Urcă pe GitHub
2. Conectează pe Render.com ca "Background Worker"
3. Adaugă environment variables:
   - BITGET_API_KEY
   - BITGET_SECRET_KEY
   - BITGET_PASSPHRASE
