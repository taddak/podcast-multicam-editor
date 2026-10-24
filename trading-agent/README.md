# Autonomní trading agent (10 € na týden)

Agent sám obchoduje spotové kryptoměny na regulované EU burze. Dáš mu malý rozpočet (třeba 10 €) a dobu běhu (třeba 168 h = týden). On pak:

- každou minutu hlídá otevřené pozice (stop-loss, trailing stop, take-profit, pojistky na celý účet),
- zhruba každé 1–6 hodin (tempo si volí sám) se podívá na trh a rozhodne, co koupit nebo prodat,
- všechno zapisuje do deníku, po restartu pokračuje tam, kde skončil, a na konci týdne vše prodá a vypíše výsledek.

Rozhoduje buď **Claude** (`claude-opus-5-5` přes Anthropic API), nebo zdarma jednoduchá **pravidlová strategie** (sledování trendu). Žádné rozhodnutí mozku ale neobejde **risk manager**: limity v kódu platí vždycky.

---

## Nejdřív upřímně

- **Zisk není jistý, ztráta je možná.** Na týdenním horizontu je krátkodobý pohyb kryptoměn z velké části náhodný. Ani Claude, ani žádná strategie nemá zaručenou výhodu. Rozumné očekávání je výsledek někde mezi zhruba −15 % a +15 %. Pojistka vypne agenta při propadu o 25 % od maxima.
- **Poplatky jsou u 10 € hlavní nepřítel.** Jeden nákup a prodej stojí na Bitvavo kolem 0,7 % (2× 0,25 % poplatek + spread + skluz). Dvacet obchodů za týden tak sní ~14 % kapitálu. Agent to ví a má pokyn „neobchodovat zbytečně“. Backtest pravidlové strategie na syntetických datech: 42 obchodů za měsíc, poplatky 2,12 € z 10 €.
- **Minimální objednávky.** Bitvavo bere objednávky od 5 €. Agent otevírá pozici aspoň za 1,3× minimum (6,50 €), aby šla prodat i po poklesu. Při 10 € tedy drží **nejvýš jednu pozici najednou**.
- **Claude API stojí peníze.** Jedno rozhodnutí s Opus 5.5 vyjde zhruba na 0,03–0,10 $. Agent má tvrdý strop `llm_budget_usd = 5`, vidí, kolik už utratil, a podle toho si volí tempo rozhodování. Když rozpočet dojde, převezme to pravidlová strategie. Při 10 € kapitálu jsou tak náklady na „mozek“ srovnatelné s celým kapitálem. Ber to jako experiment, ne jako výdělek. Levnější varianty: `--model claude-haiku-5-5`, nebo `--brain rules` (zdarma).
- Testováno je vše offline (simulace, falešná burza, falešné Claude API). Na skutečné burzy jsem se z vývojového prostředí připojit nemohl. Proto nejdřív spusť `check` a týden **paper** režimu, a teprve potom živé peníze.

---

## Výběr brokera (stav k říjnu 2026)

Pro 10 € dávají smysl jen **krypto burzy**. Fungují 24/7, berou malé objednávky a mají dobré API. Agent se k burzám připojuje přes knihovnu [ccxt](https://github.com/ccxt/ccxt), takže umí přes 100 burz. Tyhle jsem prověřil:

| Burza | `exchange.id` | Regulace v EU | Poplatky (nejnižší úroveň) | Min. objednávka | Verdikt |
|---|---|---|---|---|---|
| **Bitvavo** | `bitvavo` | MiCA licence (AFM, NL) | 0,15 % maker / 0,25 % taker | 5 € | **Doporučuji.** EUR páry, SEPA zdarma, nízké poplatky. Výchozí volba. |
| Kraken | `kraken` | MiCA licence (Centrální banka Irska) | zdroje se rozcházejí: po přestavbě ceníku v červenci 2026 výrazně dražší (až 0,40 % / 0,80 %) | v jednotkách coinu, cca 2–10 € | Spolehlivé API. Před použitím ověř aktuální poplatky. |
| OKX (EEA) | `myokx` | MiCA licence (Malta) | cca 0,10–0,35 % (nepotvrzeno) | velmi malé | Páry v EUR/USDC (USDT v EU není). Alternativa, pokud chceš menší pozice. |
| Bybit EU | `bybiteu` | MiCA licence (Rakousko) | od 5. 10. 2026 údajně 0,25 % paušálně | – | Použitelné, bez výhody oproti Bitvavo. |
| Binance | – | MiCA licenci nezískal, od 1. 7. 2026 omezuje služby v EU | – | – | **Nedoporučuji.** Hrozí zablokování účtu nebo služeb. |

Proč ne akcie:

- **Trading 212** má API jen v betě, pro živý účet jen tržní příkazy a obchoduje jen v otevírací době burz. Přidat ho jako další „burzu“ by šlo, ale pro týdenní experiment je krypto praktičtější.
- **Alpaca** podle dostupných informací neotevírá retailové účty pro rezidenty ČR (EU pobočka cílí na fintech partnery).
- **Interactive Brokers** má minimální poplatek ~1,25 € za obchod. To je 12 % z 10 €.

Čísla o poplatcích pochází z veřejných zdrojů a mění se. Příkaz `python -m trader check` ukáže minimální objednávky a poplatky, které hlásí burza. Skutečný poplatek svého účtu můžeš nastavit v `config.toml` (`taker_fee`).

Zdroje: [Bitvavo – poplatky](https://www.bitvavo.com/en/fees), [Kraken – poplatky](https://support.kraken.com/articles/201893638-fee-schedule), [přehled MiCA burz (Tangem)](https://tangem.com/en/learning-hub/post/best-mica-licensed-exchanges-europe/), [Binance a MiCA (Payment Expert)](https://paymentexpert.com/2026/06/17/binance-mica-licence-denied-europe/), [Bybit EU vs OKX](https://cryptoticker.io/en/comparison/bybit-vs-okx/), [Trading 212 API](https://docs.trading212.com), [Alpaca v Evropě](https://www.fintechfutures.com/press-releases/alpaca-expands-into-europe-with-wealthkernel-acquisition-and-launch-of-european-equities-trading).

---

## Jak to funguje

```
každou minutu (tick)                                každých 30–360 min (rozhodnutí)
┌──────────────────────────────┐                    ┌──────────────────────────────────────────┐
│ ceny držených pozic          │                    │ ceny + svíčky 1h celého trhu             │
│ trailing stop, stop-loss, TP │                    │ → tabulka: změny 1h/4h/24h/7d, RSI, EMA, │
│ denní limit ztráty           │                    │   ATR, spread, objem, min. pozice        │
│ kill switch (−25 % od max.)  │                    │ → mozek (Claude nebo pravidla)           │
└──────────────┬───────────────┘                    │   Claude si může dotáhnout svíčky 15m–1d │
               │                                    │ → JSON rozhodnutí (vynucené schéma)      │
               ▼                                    └───────────────────┬──────────────────────┘
       RISK MANAGER  ◄──────────────────────────────────────────────────┘
       (min. objednávka, max. pozic, limit obchodů/den, cooldown, spread, rozpočet)
               │
               ▼
       burza (paper = simulované plnění na reálných cenách, live = skutečné příkazy)
               │
               ▼
       runs/<název>/journal.jsonl, state.json, agent.log
```

- **Claude mozek** (`trader/brains/claude.py`): každé rozhodnutí je krátká samostatná konverzace. Claude dostane stav účtu, pozice, tabulku trhu, poslední obchody, svá poslední rozhodnutí a poznámky, které si minule nechal. Může zavolat nástroj `get_candles` (volitelně i hledání zpráv na webu). Odpoví strukturovaným JSONem: nákupy a prodeje, stop-loss, take-profit, poznámky pro příště a kdy chce rozhodovat znovu. Na pozadí běží serverový fallback pro případ, že by bezpečnostní klasifikátor požadavek odmítl.
- **Pravidlový mozek** (`trader/brains/rules.py`): kupuje v potvrzeném hodinovém uptrendu (EMA20 > EMA50, RSI 52–70, nekupuje přetažené svíčky), stop nastaví na 2× ATR a cíl na 2,5násobek stopu. Prodává, když se trend otočí. Zdarma, slouží pro backtest a jako záloha.
- **Risk manager** (`trader/risk.py`): jen spot a jen long, žádná páka. Kontroluje minimální velikost pozice, max. počet pozic a obchodů za den, cooldown po stop-lossu, maximální spread, rozsah stop-lossu (1–8 %), denní limit ztráty (−8 % = stop nákupů do půlnoci UTC) a kill switch (−25 % od maxima = prodat vše a skončit).
- **Vlastní účetnictví:** agent počítá jen s přiděleným kapitálem, i když máš na burze víc. V živém režimu se navíc srovnává se skutečnými zůstatky.

---

## Instalace

Potřebuješ Python 3.11 nebo novější.

```bash
cd trading-agent
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.toml config.toml
cp .env.example .env               # doplň klíče
```

## Postup krok za krokem

**1. Kontrola spojení a minimálních objednávek** (bez klíčů na burzu, jen veřejná data):

```bash
python -m trader check --brain rules
```

Vypíše ceny, spread, objem, minimální objednávku, minimální pozici a poplatek pro každý pár. Řekne taky, kolik pozic se do tvého kapitálu vejde. S `ANTHROPIC_API_KEY` v `.env` ověří i přístup ke Claude.

**2. Backtest pravidlové strategie na historii** (zdarma):

```bash
python -m trader backtest --days 30
```

Stáhne 30 dní hodinových svíček, přehraje je přes stejného agenta a porovná výsledek s „koupit a držet“.

**3. Týden na papíře** (reálné ceny, simulované obchody, žádné peníze na burze):

```bash
python -m trader run --name paper-week                 # Claude (potřebuje ANTHROPIC_API_KEY)
python -m trader run --name paper-rules --brain rules  # zdarma
```

Průběžný stav uvidíš v jiném terminálu:

```bash
python -m trader report --name paper-week
```

**4. Ostrý týden s 10 €** (až po paper týdnu):

1. Založ si účet na [Bitvavo](https://bitvavo.com), ověř identitu a pošli 10 € přes SEPA.
2. V nastavení vytvoř API klíč **jen s právem obchodovat (Trade), bez výběrů (Withdraw)**. Pokud to jde, omez ho na IP adresu stroje, kde agent poběží.
3. Doplň `EXCHANGE_API_KEY` a `EXCHANGE_API_SECRET` do `.env`.
4. Spusť:

```bash
python -m trader check                          # ověří klíč a zůstatek
python -m trader run --live --name live-week1 --capital 10
```

Agent se ještě zeptá, jestli opravdu obchodovat se skutečnými penězi (napiš `YES`). Pro běh na pozadí přidej `--yes-real-money`.

## Kde to nechat běžet týden

Agent musí běžet nepřetržitě, jinak nehlídá stop-lossy. Stav se ukládá po každém kroku. Když se proces zastaví (Ctrl+C, restart, výpadek), stačí spustit **stejný příkaz se stejným `--name`** a agent pokračuje. Možnosti:

- **Vlastní PC / notebook:** vypni uspávání. Nejjednodušší je spustit to v `tmux`/`screen`, nebo na Windows v samostatném okně.
- **Raspberry Pi** nebo domácí server.
- **Malý cloudový server** (free tier Oracle Cloud / Google Cloud e2-micro, nebo VPS za pár eur). Placený VPS je ale u 10 € kapitálu znatelný náklad.
- **Docker:**

  ```bash
  docker build -t trader .
  docker run -d --name trader --restart unless-stopped --env-file .env \
      -v "$PWD/runs:/app/runs" -v "$PWD/config.toml:/app/config.toml:ro" \
      trader run --live --yes-real-money --name live-week1
  docker logs -f trader
  ```

## Výstupy

V `runs/<název>/` najdeš:

- `agent.log`: čitelný log (rozhodnutí, nákupy, prodeje, chyby),
- `journal.jsonl`: kompletní deník (každé rozhodnutí včetně zdůvodnění, spotřeby tokenů a ceny, každý obchod, křivka equity),
- `state.json`: aktuální stav pro pokračování po restartu.

`python -m trader report --name <název> --csv` vypíše souhrn (výnos, max. propad, úspěšnost obchodů, poplatky, náklady na Claude) a uloží `equity.csv` pro graf.

## Nejdůležitější nastavení (`config.toml`)

| Klíč | Výchozí | Význam |
|---|---|---|
| `run.capital` | 10 | Kolik € smí agent použít. |
| `run.duration_hours` | 168 | Délka běhu. |
| `run.close_all_at_end` | true | Na konci vše prodat. |
| `exchange.id` | bitvavo | Burza (ccxt id). |
| `exchange.symbols` | 12 velkých coinů v EUR | Ze kterých trhů vybírá; nelikvidní automaticky přeskočí. |
| `risk.max_drawdown_pct` | 25 | Kill switch od maxima equity. |
| `risk.daily_loss_limit_pct` | 8 | Po této denní ztrátě už ten den nenakupuje. |
| `risk.max_trades_per_day` | 8 | Brzda proti přeobchodování. |
| `brain.type` | claude | `claude` nebo `rules`. |
| `brain.model` | claude-opus-5-5 | Levnější: `claude-sonnet-5-5`, `claude-haiku-5-5`. |
| `brain.llm_budget_usd` | 5 | Tvrdý strop útraty za Claude API za celý běh. |
| `brain.web_search` | false | Povolí Claudovi hledat zprávy (~0,01 $ za hledání + tokeny). |

Většinu jde přepsat i z příkazové řádky (`--capital`, `--hours`, `--brain`, `--model`, `--budget`, `--exchange`, `--symbols`, `--web-search`). Viz `python -m trader run --help`.

## Bezpečnost

- API klíč burzy **nikdy s právem výběru**. Agent výběry nepotřebuje.
- Klíče patří jen do `.env`. Ten je v `.gitignore`, necommituj ho.
- Na burze ideálně jen částka určená pro experiment (nebo samostatný subúčet). Nikdy ale nepoužije víc než `capital`.
- Živý režim vyžaduje přepínač `--live`, API klíče a výslovné potvrzení.
- Když se při výpadku spojení nepodaří potvrdit nákup, který burza přesto provedla, agent o koupeném coinu neví. Víc peněz kvůli tomu neutratí, protože hotovost si vždy ověřuje proti zůstatku na burze. Takový coin ale nebude hlídat. Po skončení běhu se proto podívej na účet na burze.

## Testy

```bash
pip install pytest
python -m pytest -q
```

Testy pokrývají indikátory, účetnictví, risk manager, parsování plnění z burzy, celý běh agenta na syntetických datech, pokračování po restartu, kill switch, backtest, živou cestu proti falešnému ccxt klientovi a Claude mozek proti falešnému API (smyčka nástrojů, parsování, rozpočet, odmítnutí).

*Nejde o investiční doporučení. Obchodování s kryptoměnami je riskantní. Počítej s tím, že o vložené peníze můžeš přijít.*
