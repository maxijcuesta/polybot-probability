# STATUS — probabilisticobot

**Fecha:** 2026-04-06
**Modo activo:** paper trade (default)
**Rama:** main

---

## Estado general

El sistema está **completo a nivel de arquitectura** y listo para correr en paper mode.
No hay features rotas ni deuda técnica crítica abierta. La capa de analytics/auditoría
está terminada y es coherente con el pipeline de ejecución.

---

## Qué está funcionando

| Componente | Estado |
|---|---|
| Ciclo principal (`BotCycle.run_once`) | ✓ Funcional |
| Fetcher de mercados (Gamma API pública) | ✓ Funcional (no requiere auth) |
| Guards operacionales | ✓ Funcional |
| Feature builder | ✓ Funcional |
| NaiveModel (prob implícita en precio) | ✓ Funcional |
| Calibración automática (Identity → Isotonic) | ✓ Se activa con ≥ 30 trades cerrados |
| Signal engine + FillModel | ✓ Funcional |
| Risk engine (Kelly fraccionario, 7 checks duros + 4 trims) | ✓ Funcional |
| Paper execution engine | ✓ Funcional |
| Exit engine (take profit, stop loss, max hold, trailing stop) | ✓ Funcional |
| DB SQLite (trades, signals, funnel_events, market_snapshots) | ✓ Funcional |
| Migraciones idempotentes | ✓ Funcional |
| Funnel tracking EAV (10 contadores por ciclo) | ✓ Funcional |
| `scripts/funnel_audit.py` (4 secciones + capital) | ✓ Funcional |
| `scripts/audit_model.py` (diagnóstico teórico) | ✓ Funcional |
| Dashboard aiohttp (puerto 8080) | ✓ Funcional |
| `run_bot.py` (CLI con --once / --metrics / --dashboard) | ✓ Funcional |

---

## Qué NO está demostrado aún

| Pregunta | Estado |
|---|---|
| ¿El NaiveModel genera edge real? | Sin datos — necesita ciclos reales |
| Hit rate (% ganadores) | Sin datos — necesita ≥ 30 trades cerrados |
| PnL positivo en paper mode | Sin datos |
| Calibración real (Brier score, log loss) | Sin datos — requiere resolución de mercados |
| Qué cohortes tienen edge post-costo | Sin datos — correr funnel_audit.py con DB poblada |
| Si el risk engine es el cuello de botella | Sin datos — revisar sección B2 del audit |

**Nota:** La ausencia de datos no es un bug. El sistema está diseñado para acumular
evidencia en paper mode antes de cualquier evaluación estratégica.

---

## Invariantes de funnel

Documentados en `src/polybot/jobs/cycle.py`. Se verifican en `scripts/smoke_test.py`.

```
# Identidad por ciclo (modulo excepciones):
already_positioned + risk_approved + risk_rejected = positive_edge_net

# Orden lógico:
executed          ≤ risk_approved
exited            ≤ executed  (open → cerrado, no al revés)
winners + losers  = resolved  (todos los cerrados tienen outcome)

# Sizing:
approved_size_usd ≤ requested_size_usd  (nunca se aprueba más de lo pedido)
```

---

## Thresholds de paper trading (cloud)

Los umbrales en `config.cloud.toml` fueron bajados deliberadamente para destrabar la
observación de paper trades reales. **Esto no implica que la estrategia esté validada.**

| Parámetro | Default (`config.example.toml`) | Cloud (`config.cloud.toml`) | Motivo |
|---|---|---|---|
| `model.min_edge_raw` | 0.04 | **0.02** | NaiveModel techo ≈ 0.035; con 0.04 nunca pasa |
| `model.min_edge_net` | 0.02 | **0.005** | Margen neto realista para paper mode |
| `guards.min_volume_24h_usd` | 10 000 | **2 500** | Abre universo a mercados reales con menos volumen |

Una vez acumulados ≥ 30 paper trades cerrados, revisar estos valores con
`scripts/funnel_audit.py` y ajustar basado en evidencia real, no en supuestos.

---

## Veredicto actual

> **Sistema apto para paper trading. Demasiado pronto para evaluar la estrategia.**

El NaiveModel usa la probabilidad implícita en el precio de mercado como baseline.
Por construcción, su edge esperado es cercano a cero antes de calibración. Se necesitan
ciclos reales para saber si los guards están bien calibrados y si hay cohortes con
edge positivo post-costo.

---

## Flujo mínimo reproducible

### 1. Instalar dependencias

```bash
pip install aiohttp aiosqlite structlog
```

### 2. Copiar configuración

```bash
cp config.example.toml config.toml
```

La config por defecto usa `paper_trade = true` y `dry_run = true`.
No se necesitan credenciales para paper mode — el fetcher usa la API pública de Gamma.

### 3. Correr un ciclo

```bash
python3.12 run_bot.py --once
```

Output esperado: dict con `status: ok`, contadores de funnel, posiciones abiertas.

### 4. Correr el funnel audit

```bash
python scripts/funnel_audit.py --db ./data/polybot.db
```

Con DB vacía (primer ciclo): muestra pirámide con datos reales del ciclo ejecutado.

### 5. Smoke test (CI / verificación rápida)

```bash
python scripts/smoke_test.py
```

Corre un ciclo, verifica invariantes en DB, imprime PASS / FAIL. Exit 0 si todo ok.

---

## Próximos pasos mínimos

1. **Correr en paper mode 48–72h** (`python3.12 run_bot.py` con `scan_interval_seconds = 60`)
2. **Revisar funnel_audit.py** con DB poblada → identificar etapa donde mueren las señales
3. **Coleccionar ≥ 30 trades cerrados** → la calibración se activa sola
4. **Ajustar guards** según cohortes (spread_pct, bid_depth_usd, hours_to_resolution)
5. **Evaluar hit rate y PnL** solo después del paso 3

No agregar modelos ML ni features nuevas hasta tener evidencia de la sección C del audit.

---

## Archivos clave

```
run_bot.py                          # entry point
config.example.toml                 # template de configuración
src/polybot/
  jobs/cycle.py                     # ciclo principal (orquestación)
  risk_engine/sizer.py              # Kelly + caps
  signal_engine/engine.py           # señales + FillModel
  execution_engine/paper.py         # ejecución paper
  db.py                             # capa de persistencia
  models.py                         # todos los tipos de dominio
  config.py                         # BotConfig (dataclass puro)
scripts/
  funnel_audit.py                   # auditoría completa
  audit_model.py                    # diagnóstico teórico del modelo
  smoke_test.py                     # verificación mínima (CI)
```
