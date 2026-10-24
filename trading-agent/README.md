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
- **Claude API stojí peníze.** Jedno rozhodnutí s Opus 5.5 vyjde zhruba na 0,03–0,10 $. Agent má tvrdý strop `llm_budget_usd = 5`, vidí, kolik už utratil, a podle toho si volí tempo rozhodování. Když rozpočet dojde, převezme to pravidlová strategie. Při 10 € kapitálu jsou tak náklady na „mozek“ srovnatelné s celým kapitálem. Ber to jako experiment, ne jako výdělek. S předplatným Max to pokryje měsíční API kredit, který je v ceně (viz [Claude: API klíč vs. předplatné](#claude-api-klíč-vs-předplatné)). Levnější varianty: `--model claude-haiku-5-5`, nebo `--brain rules` (zdarma).
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

## Instalace a zapojení

Podrobný návod krok za krokem (instalace, Claude klíč, burza, ostrý běh) je v **[ZACINAME.md](ZACINAME.md)**. Ve zkratce:

```bash
./setup.sh                                  # Windows: powershell -ExecutionPolicy Bypass -File .\setup.ps1
source .venv/bin/activate                   # Windows: .\.venv\Scripts\Activate.ps1
python -m trader keys                       # API klíče se zadávají skrytě, uloží se jen do soukromého .env
python -m trader check                      # bezpečnostní kontrola, data z burzy, ověření klíčů
python -m trader backtest --days 30         # pravidlová strategie na historii (zdarma)
python -m trader run --name paper-week      # týden na papíře
python -m trader run --live --name live-week1 --capital 10   # ostrý týden
python -m trader report --name live-week1   # průběžný nebo konečný výsledek
python -m trader close-all --live --name live-week1          # nouzové tlačítko: prodat vše a skončit
```

## Kde to nechat běžet týden

Agent musí běžet nepřetržitě, jinak nehlídá stop-lossy. Stav se ukládá po každém kroku. Když se proces zastaví (Ctrl+C, restart, výpadek), stačí spustit **stejný příkaz se stejným `--name`** a agent pokračuje. Možnosti:

- **Vlastní PC nebo notebook:** vypni uspávání. Nejjednodušší je spustit agenta v `tmux` nebo `screen`, na Windows v samostatném okně.
- **Raspberry Pi** nebo domácí server.
- **Docker** (nejlepší izolace): kontejner nevidí nic jiného z tvého počítače, běží pod neprivilegovaným uživatelem a klíče dostává až při spuštění, nikdy nejsou v obrazu.

  ```bash
  docker build -t trader .
  docker run -d --name trader --restart unless-stopped \
      --env-file .env --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
      -v trader-runs:/app/runs -v "$PWD/config.toml:/app/config.toml:ro" \
      trader run --live --yes-real-money --name live-week1
  docker logs -f trader
  docker run --rm -v trader-runs:/app/runs trader report --name live-week1
  ```

## Výstupy

V `runs/<název>/` najdeš:

- `agent.log`: čitelný log (rozhodnutí, nákupy, prodeje, chyby),
- `journal.jsonl`: kompletní deník (každé rozhodnutí včetně zdůvodnění, spotřeby tokenů a ceny, každý obchod, křivka equity),
- `state.json`: aktuální stav pro pokračování po restartu.

Na macOS a Linuxu jsou všechny tyto soubory čitelné jen pro tvého uživatele. `python -m trader report --name <název> --csv` vypíše souhrn (výnos, max. propad, úspěšnost obchodů, poplatky, objem prodejů, náklady na Claude) a uloží `equity.csv` pro graf.

## Nejdůležitější nastavení (`config.toml`)

| Klíč | Výchozí | Význam |
|---|---|---|
| `run.capital` | 10 | Kolik € smí agent použít. |
| `run.max_live_capital` | 50 | Pojistka: ostrý běh s vyšším kapitálem odmítne (proti překlepu). |
| `run.duration_hours` | 168 | Délka běhu. |
| `run.close_all_at_end` | true | Na konci vše prodat. |
| `exchange.id` | bitvavo | Burza (ccxt id). |
| `exchange.symbols` | 12 velkých coinů v EUR | Ze kterých trhů vybírá; nelikvidní automaticky přeskočí. Stablecoiny jsou zakázané. |
| `risk.max_drawdown_pct` | 25 | Kill switch od maxima equity. |
| `risk.daily_loss_limit_pct` | 8 | Po této denní ztrátě už ten den nenakupuje. |
| `risk.max_trades_per_day` | 8 | Brzda proti přeobchodování. |
| `brain.type` | claude | `claude` nebo `rules`. |
| `brain.model` | claude-opus-5-5 | Levnější: `claude-sonnet-5-5`, `claude-haiku-5-5`. |
| `brain.llm_budget_usd` | 5 | Tvrdý strop útraty za Claude API za celý běh. |
| `brain.web_search` | false | Povolí Claudovi hledat zprávy (~0,01 $ za hledání + tokeny). |

Většinu jde přepsat i z příkazové řádky (`--capital`, `--hours`, `--brain`, `--model`, `--budget`, `--exchange`, `--symbols`, `--web-search`). Viz `python -m trader run --help`.

---

## Bezpečnost

Stoprocentní bezpečnost neexistuje u žádného softwaru. Agent je proto postavený tak, aby **i nejhorší případ měl malý a předem známý dopad**: na burze je jen 10 €, klíč z ní nemůže nic vybrat a Claude API má strop útraty.

### Co se stane, když…

| Scénář | Co agent nebo nastavení udělá | Nejhorší dopad |
|---|---|---|
| Někdo ukradne `.env` s klíči | Klíč burzy nemá právo výběru, takže peníze z účtu nikam nepošle. S IP whitelistem z cizího počítače vůbec nefunguje. Anthropic klíč brzdí spend limit workspace. | Nesmyslné obchody s 10 € na účtu, útrata do limitu workspace. Klíče hned smaž a vytvoř nové. |
| Chyba v kódu nebo „zblázněný“ Claude | Risk manager pustí jen spotové nákupy a prodeje do přiděleného kapitálu. Výběry, převody, páka a margin jsou v kódu zablokované, ani s „chybným“ klíčem je nejde zavolat. Kill switch při −25 %. | Ztráta části z 10 €. |
| Podvržený balíček v knihovnách (supply chain) | Instalace přes `requirements.lock`: každý soubor má otištěný SHA-256 hash a pozměněný balíček pip odmítne. Přednostně se instalují hotové balíčky, takže se nespouštějí jejich instalační skripty. | – |
| Škodlivý obsah na webu (prompt injection) | Hledání na webu je ve výchozím stavu vypnuté. I když ho zapneš, Claude dostal pokyn brát web jen jako informaci. Jeho výstup navíc umí jen nákup nebo prodej v rámci limitů a nemá přístup k souborům ani příkazům. | Špatný obchod v rámci limitů. |
| Podstrčený `.env` nebo proměnné prostředí | Z `.env` se načtou jen 4 klíče, nic jiného (třeba přesměrování API jinam se ignoruje). `check` upozorní na proxy a přesměrování. | – |
| Klíče v logu | Hodnoty klíčů se v logu, deníku i chybových hláškách nahrazují `***`. | – |

### Kam data odcházejí

- **Burza (Bitvavo):** dotazy na ceny a tvoje příkazy k nákupu a prodeji.
- **Anthropic:** tabulka trhu a stav agentova účtu (částky, pozice). Žádné jméno, e-mail ani číslo účtu. Data z API Anthropic podle obchodních podmínek nepoužívá k trénování modelů.
- **Nic jiného.** Agent nemá žádný otevřený port ani server, žádnou telemetrii a nečte žádné jiné tvoje soubory. V Dockeru je to vynucené i technicky.

### Co uděláš ty

- **Na burze** klíč jen s právem **View + Trade**, nikdy **Withdraw**. Pokud máš stálou IP adresu, nastav i IP whitelist.
- **Zapni 2FA** (aplikace, ne SMS) na burze, v Anthropic Console a na e-mailu, ke kterému jsou účty vázané.
- **V Console** nastav spend limit na workspace s klíčem agenta.
- **Klíče zadávej jen** přes `python -m trader keys`. Nikdy do chatu, e-mailu ani kódu. `.env` necommituj. Agent ostrý běh odmítne, když zjistí, že je `.env` v gitu.
- **Na burze měj jen** částku na experiment.
- **Po skončení** klíče smaž.
- **Známé omezení:** když se při výpadku spojení nepodaří potvrdit nákup, který burza přesto provedla, agent o koupeném coinu neví. Víc peněz kvůli tomu neutratí, protože hotovost si vždy ověřuje proti zůstatku na burze. Takový coin ale nebude hlídat. Po běhu se proto podívej na účet na burze.

Kód prošel bezpečnostní revizí (secrets, injection, deserializace, TLS, cesty k souborům), statickou analýzou `bandit` a kontrolou závislostí na známé zranitelnosti (`pip-audit`). Revize nenašla žádnou zranitelnost. Dva drobné nálezy z nástrojů jsou opravené: neviditelné „bidi“ znaky přímo ve zdrojovém kódu filtru a zranitelná verze testovací knihovny pytest.

## Claude: API klíč vs. předplatné

- **Předplatné (Pro/Max) a API jsou dvě různé věci.** Agent je program, který běží sám, takže používá **API klíč**. Na API klíč se vztahují obchodní podmínky Anthropicu (Commercial Terms) a automatizace je u něj normální, povolené použití.
- **Max:** v ceně je **měsíční API kredit 100 $ (Max 5x) nebo 200 $ (Max 20x)**. Zapneš ho na claude.ai v **Settings → Billing → API credits → Link organization**. Kredit se čerpá přes API klíč z propojené organizace, takže agent poběží „v rámci plánu“. Nevyčerpaný kredit koncem období propadá. Když dojde, API se zastaví (pokud nemáš dokoupené kredity) a agent přejde na pravidla. Z předplatného se nic navíc nestrhne.
- **Pro:** API kredit v ceně není. V Console si předplatíš kredit. Na týden experimentu stačí 5–10 $.
- **Proč ne přes přihlášení k předplatnému** (`claude -p`): spotřebitelské podmínky povolují automatizovaný přístup jen přes API klíč, nebo kde to Anthropic výslovně povolí. Čerpalo by to limity tvého plánu a model by dostal i nástroje Claude Code (soubory, příkazy), což je zbytečné riziko. Agent to proto nedělá.

Podrobnosti: [Monthly API credits for Max and Team plans](https://support.claude.com/en/articles/17154008-monthly-api-credits-for-max-and-team-plans), [Using Claude Code with your Pro or Max plan](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan), [Spend limits](https://platform.claude.com/docs/en/api/rate-limits).

## Zákony a daně v ČR

*Shrnutí k říjnu 2026. Není to právní ani daňové poradenství. V případě pochybností se obrať na daňového poradce.*

- **Licencovaná burza:** Bitvavo má licenci podle evropského nařízení MiCA (dohled nizozemského AFM). Ta platí v celé EU a Česko je mezi podporovanými zeměmi. Nepoužívej burzy, které v EU licenci MiCA nemají.
- **Jen vlastní peníze:** obchodovat za sebe je legální. Agentem ale **nespravuj peníze jiných lidí** a nenabízej ho jako službu. Správa cizích prostředků a podobné služby jsou licencované činnosti.
- **Žádná manipulace trhem:** MiCA zakazuje manipulaci s trhem (například fiktivní obchody nebo klamné příkazy). Agent dává jen běžné tržní příkazy na likvidních trzích a nic takového nedělá.
- **Daně:**
  - Od 15. 2. 2025 je příjem z prodeje kryptoaktiv **osvobozený od daně**, pokud **úhrn příjmů z prodejů za rok nepřesáhne 100 000 Kč**, nebo pokud jsi kryptoaktivum držel déle než 3 roky.
  - Počítají se **hrubé příjmy z prodejů (ne zisk)** ze všech tvých prodejů kryptoměn za rok.
  - **Stablecoiny** (elektronické peněžní tokeny) osvobození nemají, a proto je agent nemá povolené.
  - Týden s 10 € znamená řádově stovky eur objemu prodejů, tedy hluboko pod limitem. Pokud letos jinde neprodáváš krypto za víc, nic se nepřiznává.
  - `report` ukazuje objem prodejů (řádek „sales … gross proceeds“), abys to měl přehledně.
- **Hlášení burz:** podle směrnice DAC8 sbírají burzy od 1. 1. 2026 údaje o transakcích klientů a předávají je daňovým správám. Na tvých povinnostech to nic nemění.
- **Claude:** přes API klíč je automatizace v souladu s podmínkami Anthropicu (viz výše).

Zdroje: [Bitvavo – podporované země](https://support.bitvavo.com/hc/en-us/articles/4405238046993), [Bitvavo – API klíče](https://support.bitvavo.com/hc/en-us/articles/4405059841809), [Osvobození příjmů z kryptoaktiv (DAUC)](https://www.dauc.cz/clanky/17373/osvobozeni-prijmu-z-uplatneho-prevodu-kryptoaktiv), [Forvis Mazars – osvobození krypta](https://www.forvismazars.com/cz/cs/prehledy/newslettery/newslettery-z-danove-oblasti/forvis-mazars-tax-view-archiv/osvobozeni-prijmu-z-prodeje-kryptoaktiv), [Grant Thornton – DAC8](https://www.grantthornton.cz/en/news/dac-8-new-obligation-for-crypto-platforms-and-their-users-in-2026/), [Anthropic Consumer Terms](https://www.anthropic.com/legal/consumer-terms).

## Testy

```bash
pip install --require-hashes -r requirements-dev.lock    # setup skript to dělá sám
python -m pytest -q
```

Testy (38) pokrývají:

- indikátory, účetnictví a risk manager,
- parsování plnění z burzy,
- celý běh agenta na syntetických datech, pokračování po restartu, kill switch, nouzové tlačítko a backtest,
- živou cestu proti falešnému ccxt klientovi,
- Claude mozek proti falešnému API (smyčka nástrojů, parsování, rozpočet, odmítnutí),
- bezpečnost: maskování klíčů v logu a deníku, oprávnění `.env`, `.env` v gitu, blokované výběry, pojistku kapitálu, filtr řídicích znaků a bezpečné zadávání klíčů.

*Nejde o investiční doporučení. Obchodování s kryptoměnami je riskantní. Počítej s tím, že o vložené peníze můžeš přijít.*
