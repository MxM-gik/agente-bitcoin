import ccxt
import time
import threading
from datetime import datetime, timedelta

# ============================================
# AGENTE MULTI-MERCADO — MODO SERVIDOR
# Sin interfaz gráfica — solo trading + Telegram
# ============================================

CAPITAL_INICIAL = 15000
CLP_POR_USD     = 950

CAIDA_LEVE   = 0.01
CAIDA_MEDIA  = 0.03
CAIDA_FUERTE = 0.05
CAPITAL_CAIDA_LEVE   = 0.30
CAPITAL_CAIDA_MEDIA  = 0.60
CAPITAL_CAIDA_FUERTE = 0.89

SALIDA_MINIMA = 0.005
SALIDA_MEDIA  = 0.015
SALIDA_FUERTE = 0.03

COMISION               = 0.001
STOP_LOSS_DIARIO       = 0.20
STOP_LOSS_CATASTROFICO = 0.18
CAIDA_REACTIVA         = 0.02

MERCADOS_CONFIG = [
    {"simbolo": "BTC/USDT", "nombre": "Bitcoin",  "icono": "₿"},
    {"simbolo": "ETH/USDT", "nombre": "Ethereum", "icono": "Ξ"},
    {"simbolo": "BNB/USDT", "nombre": "BNB",      "icono": "◈"},
]

CAPITAL_POR_MERCADO = {
    "BTC/USDT": int(CAPITAL_INICIAL * 0.50),
    "ETH/USDT": int(CAPITAL_INICIAL * 0.30),
    "BNB/USDT": int(CAPITAL_INICIAL * 0.20),
}

exchange = ccxt.binance()

TELEGRAM_TOKEN   = "8722467841:AAENHyHPMxFkWZSuBkqUFRKMqraM7Vbmcwg"
TELEGRAM_CHAT_ID = "1523499171"

def telegram(msg):
    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                                 "parse_mode": "HTML"}, timeout=10)
    except Exception:
        pass

class MercadoState:
    def __init__(self, cfg):
        self.simbolo   = cfg["simbolo"]
        self.nombre    = cfg["nombre"]
        self.icono     = cfg["icono"]
        cap = CAPITAL_POR_MERCADO[self.simbolo]
        self.capital_disponible  = cap
        self.capital_inicial     = cap
        self.ganancia_total      = 0
        self.operaciones         = 0
        self.capital_recuperado  = False
        self.posiciones          = []
        self.precio_base         = 0
        self.historial_precios   = []
        self.buffer_lecturas     = []
        self.pausado_hasta       = None
        self.precio_ultima_venta = 0
        self.compras_consecutivas= 0
        self.ultimo_ciclo_ts     = None
        self.loop_running        = False

estados = [MercadoState(cfg) for cfg in MERCADOS_CONFIG]
agente_activo = True

def clp_a_usd(clp): return clp / CLP_POR_USD
def usd_a_clp(usd): return usd * CLP_POR_USD
def btc_total(e):   return sum(p['btc'] for p in e.posiciones)
def valor_pos_clp(e, precio): return usd_a_clp(btc_total(e) * precio)

def ganancia_pct(e, precio):
    if not e.posiciones: return 0
    total_inv = sum(p['capital_invertido_clp'] for p in e.posiciones)
    if total_inv == 0: return 0
    return (valor_pos_clp(e, precio) - total_inv) / total_inv

def log(e, msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}][{e.nombre}] {msg}", flush=True)

def simular_compra(e, precio, porcentaje_capital, motivo):
    if e.capital_disponible <= 0: return
    monto_clp    = e.capital_disponible * porcentaje_capital
    monto_usd    = clp_a_usd(monto_clp)
    activo_comp  = (monto_usd * (1 - COMISION)) / precio
    e.capital_disponible -= monto_clp
    e.posiciones.append({
        'btc': activo_comp,
        'precio_compra': precio,
        'capital_invertido_clp': monto_clp,
        'ciclos': 0
    })
    e.compras_consecutivas += 1
    log(e, f"COMPRA {motivo} | ${precio:,.2f} USD | ${monto_clp:,.0f} CLP")
    telegram(f"{e.icono} <b>COMPRA — {e.nombre}</b>\n💵 ${precio:,.2f} USD\n💰 ${monto_clp:,.0f} CLP\n📌 {motivo}")

def puede_vender(e, precio, porcentaje):
    total_inv    = sum(p['capital_invertido_clp'] for p in e.posiciones)
    inv_a_vender = total_inv * porcentaje
    if inv_a_vender <= e.capital_inicial * 0.01:
        log(e, f"VENTA BLOQUEADA — posición muy pequeña (${inv_a_vender:,.0f} CLP)")
        return False
    btc_v    = btc_total(e) * porcentaje
    neto_clp = usd_a_clp(btc_v * precio * (1 - COMISION))
    ganancia = neto_clp - inv_a_vender
    if ganancia < 20:
        log(e, f"VENTA BLOQUEADA — ganancia insuficiente (${ganancia:,.0f} CLP < $20)")
        return False
    return True

def simular_venta_parcial(e, precio, porcentaje, motivo, actualizar_base=False):
    if not e.posiciones: return
    btc_v       = btc_total(e) * porcentaje
    neto_clp    = usd_a_clp(btc_v * precio * (1 - COMISION))
    total_inv   = sum(p['capital_invertido_clp'] for p in e.posiciones)
    ganancia    = neto_clp - (total_inv * porcentaje)
    e.ganancia_total     += ganancia
    e.capital_disponible += neto_clp
    e.operaciones        += 1
    e.precio_ultima_venta = precio
    for p in e.posiciones:
        p['btc']                  *= (1 - porcentaje)
        p['capital_invertido_clp'] *= (1 - porcentaje)
    e.posiciones[:] = [p for p in e.posiciones if p['btc'] > 0.000001]
    if e.capital_disponible >= e.capital_inicial and not e.capital_recuperado:
        e.capital_recuperado = True
        log(e, "*** CAPITAL RECUPERADO — FASE 2 ***")
    signo = "+" if ganancia >= 0 else ""
    log(e, f"VENTA {motivo} | ${precio:,.2f} USD | {porcentaje*100:.0f}% | {signo}${ganancia:,.0f} CLP")
    emoji = "💰" if ganancia > 0 else "🔴"
    telegram(f"{emoji} <b>VENTA — {e.nombre}</b>\n💵 ${precio:,.2f} USD\n{signo}${ganancia:,.0f} CLP\n📌 {motivo}")
    e.compras_consecutivas = 0
    if actualizar_base:
        manana = datetime.now() + timedelta(days=1)
        e.pausado_hasta = manana.replace(hour=8, minute=0, second=0, microsecond=0)
        e.precio_base   = 0
        log(e, f"PAUSA hasta {e.pausado_hasta.strftime('%d/%m %H:%M')}")

def obtener_precio(e):
    for intento in range(3):
        try:
            return exchange.fetch_ticker(e.simbolo)['last']
        except Exception:
            if intento < 2: time.sleep(10)
    raise Exception(f"{e.nombre}: sin respuesta")

def loop_mercado(e):
    e.loop_running = True
    capital_inicio_dia = e.capital_disponible
    log(e, f"Iniciado — capital ${e.capital_disponible:,.0f} CLP")
    telegram(f"🤖 <b>{e.nombre} INICIADO</b>\nCapital: ${e.capital_disponible:,.0f} CLP")

    while agente_activo:
        try:
            if e.pausado_hasta and datetime.now() < e.pausado_hasta:
                precio = obtener_precio(e)
                e.historial_precios.append(precio)
                if len(e.historial_precios) > 60: e.historial_precios.pop(0)
                if e.precio_ultima_venta > 0:
                    caida = (e.precio_ultima_venta - precio) / e.precio_ultima_venta
                    if caida >= CAIDA_REACTIVA:
                        e.pausado_hasta = None
                        e.buffer_lecturas.clear()
                        log(e, f"PAUSA CANCELADA — cayó {caida*100:.2f}%")
                        telegram(f"⚡ <b>PAUSA CANCELADA — {e.nombre}</b>\nCaída {caida*100:.2f}%")
                        continue
                log(e, f"En pausa hasta {e.pausado_hasta.strftime('%d/%m %H:%M')} | ${precio:,.2f}")
                time.sleep(300)
                continue

            elif e.pausado_hasta and datetime.now() >= e.pausado_hasta:
                e.pausado_hasta   = None
                e.precio_base     = 0
                e.compras_consecutivas = 0
                e.buffer_lecturas.clear()
                e.historial_precios.clear()
                log(e, "Pausa terminada — reiniciando")

            precio = obtener_precio(e)
            e.ultimo_ciclo_ts = datetime.now()
            precio_anterior   = e.historial_precios[-1] if e.historial_precios else precio
            e.historial_precios.append(precio)
            if len(e.historial_precios) > 60: e.historial_precios.pop(0)

            if e.precio_base == 0:
                e.precio_base = precio
                log(e, f"Precio base: ${precio:,.2f} USD")
                telegram(f"📌 <b>BASE {e.nombre}</b>: ${precio:,.2f} USD")
                time.sleep(300)
                continue

            e.buffer_lecturas.append(precio)
            if len(e.buffer_lecturas) > 4: e.buffer_lecturas.pop(0)

            variacion       = (precio - e.precio_base) / e.precio_base
            caida           = abs(variacion) if variacion < 0 else 0
            precio_bajando  = precio < precio_anterior
            precio_subiendo = precio > precio_anterior
            caida_anterior  = max(0, (e.precio_base - precio_anterior) / e.precio_base)

            for p in e.posiciones: p['ciclos'] += 1

            # ── SALIDA ──
            if e.posiciones:
                g = ganancia_pct(e, precio)
                perdida_max = min((p['precio_compra'] - precio) / p['precio_compra'] for p in e.posiciones)
                if perdida_max >= STOP_LOSS_CATASTROFICO:
                    simular_venta_parcial(e, precio, 1.0, f"CATASTRÓFICO -{perdida_max*100:.1f}%")
                elif precio_bajando and g >= SALIDA_FUERTE:
                    if puede_vender(e, precio, 0.90):
                        simular_venta_parcial(e, precio, 0.90, f"Pico +{g*100:.2f}% >=3%", actualizar_base=True)
                elif precio_bajando and g >= SALIDA_MEDIA:
                    if puede_vender(e, precio, 0.90):
                        simular_venta_parcial(e, precio, 0.90, f"Pico +{g*100:.2f}% >=1.5%", actualizar_base=True)
                elif precio_bajando and g >= SALIDA_MINIMA:
                    if puede_vender(e, precio, 0.50):
                        simular_venta_parcial(e, precio, 0.50, f"Pico +{g*100:.2f}% >=0.5%")
                elif precio_bajando:
                    log(e, f"PRECIO BAJA — ganancia insuficiente {g*100:.3f}% — manteniendo")
                else:
                    log(e, f"MANTENIENDO {g*100:.3f}% | ${precio:,.2f}")

            # ── ENTRADA ──
            if e.capital_disponible > 0:
                if precio_subiendo and caida_anterior >= CAIDA_LEVE:
                    if e.compras_consecutivas >= 2:
                        log(e, f"ESPERANDO — 2 compras seguidas")
                        e.compras_consecutivas = max(0, e.compras_consecutivas - 1)
                    elif caida_anterior >= CAIDA_FUERTE:
                        simular_compra(e, precio, CAPITAL_CAIDA_FUERTE, f"rebote -{caida_anterior*100:.2f}% >=5%")
                    elif caida_anterior >= CAIDA_MEDIA:
                        simular_compra(e, precio, CAPITAL_CAIDA_MEDIA,  f"rebote -{caida_anterior*100:.2f}% >=3%")
                    else:
                        simular_compra(e, precio, CAPITAL_CAIDA_LEVE,   f"rebote -{caida_anterior*100:.2f}% >=1%")
                elif not precio_subiendo and caida >= CAIDA_LEVE:
                    log(e, f"SIGUE BAJANDO {caida*100:.3f}% — esperando rebote")
                elif not e.posiciones:
                    log(e, f"ESPERANDO | caída {caida*100:.3f}% insuficiente | base ${e.precio_base:,.2f}")

            # Stop loss diario
            cap_total   = e.capital_disponible + valor_pos_clp(e, precio)
            perdida_dia = (capital_inicio_dia - cap_total) / capital_inicio_dia if capital_inicio_dia > 0 else 0
            if perdida_dia >= STOP_LOSS_DIARIO:
                log(e, f"STOP LOSS DIARIO {perdida_dia*100:.1f}% — pausa 30 min")
                time.sleep(1800)
                capital_inicio_dia = cap_total

            time.sleep(300)

        except Exception as ex:
            log(e, f"Error — reintentando en 30s ({str(ex)[:50]})")
            time.sleep(30)

    e.loop_running = False


def watchdog():
    while agente_activo:
        time.sleep(60)
        for e in estados:
            if e.ultimo_ciclo_ts:
                mins = (datetime.now() - e.ultimo_ciclo_ts).seconds / 60
                if mins > 10 and not e.loop_running:
                    log(e, f"WATCHDOG — {mins:.0f} min sin lectura. Reiniciando...")
                    telegram(f"⚠️ <b>{e.nombre}</b> — reiniciando hilo")
                    hilo = threading.Thread(target=loop_mercado, args=(e,), daemon=True)
                    hilo.start()


if __name__ == "__main__":
    print("=== AGENTE MULTI-MERCADO — MODO SERVIDOR ===", flush=True)
    telegram("🚀 <b>AGENTE SERVIDOR INICIADO</b>\nBTC + ETH + BNB activos")

    for e in estados:
        hilo = threading.Thread(target=loop_mercado, args=(e,), daemon=True)
        hilo.start()
        time.sleep(5)

    hilo_wd = threading.Thread(target=watchdog, daemon=True)
    hilo_wd.start()

    # Mantener proceso vivo
    while True:
        time.sleep(60)
