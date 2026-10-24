# Začínáme: zapojení krok za krokem

Celé to zabere zhruba 30–60 minut. Nejdéle obvykle trvá ověření identity na burze. Postupuj shora dolů. Ostré peníze přijdou na řadu až v kroku 5.

**Co budeš potřebovat:**

- počítač s Windows, macOS nebo Linuxem,
- [Git](https://git-scm.com/downloads),
- [Python 3.13](https://www.python.org/downloads/) (funguje 3.11 až 3.14; na Windows při instalaci zaškrtni „Add python.exe to PATH“),
- 10 € na bankovním účtu a doklad totožnosti pro ověření na burze,
- pro Claude mozek účet v [Claude Console](https://platform.claude.com). Bez něj agent pojede zdarma s pravidlovým mozkem.

**Zlaté pravidlo:** API klíče nikdy nevkládej do chatu (ani mně), do e-mailu, do kódu ani do commitu. Zadávají se jen příkazem `python -m trader keys`, který vstup nezobrazuje a uloží ho do souboru `.env`, který může číst jen tvůj uživatel.

---

## 1. Stažení a instalace (5 minut)

```bash
git clone https://github.com/taddak/podcast-multicam-editor.git
cd podcast-multicam-editor
git checkout claude/autonomous-trading-agent-all9o5
cd trading-agent
```

macOS nebo Linux:

```bash
./setup.sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Skript:

1. vytvoří vlastní izolované prostředí `.venv`,
2. nainstaluje knihovny, přičemž **každý soubor ověří proti otištěnému SHA-256 hashi** (podvržený nebo pozměněný balíček odmítne),
3. založí `config.toml` a prázdný soukromý `.env`,
4. spustí testy.

Pak aktivuj prostředí. Tenhle krok opakuj v každém novém terminálu:

```bash
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
```

## 2. Claude API klíč (10 minut, volitelné)

Agent volá Claude přes **API klíč**. To je oficiální cesta pro automatické programy. Kdo platí, záleží na tvém předplatném:

| Předplatné | Jak se platí API |
|---|---|
| **Max 5x / Max 20x** | V ceně je měsíční API kredit **100 $ (Max 5x) / 200 $ (Max 20x)**. Na claude.ai jdi do **Settings → Billing → API credits → Link organization** a propoj ho s organizací v Console (můžeš ji tam rovnou založit). Nárok vzniká po 7 dnech na plánu. Nevyčerpaný kredit koncem období propadá. Když dojde a nemáš dokoupené kredity, API přestane odpovídat a agent přejde na pravidla. Z předplatného se nic navíc nestrhne. |
| **Pro / Free** | API kredit v ceně není. V Console (**Billing**) si předplať kredit, na tenhle experiment stačí 5–10 $. |

Pak v [Console](https://platform.claude.com):

1. Zapni si dvoufázové přihlášení (2FA).
2. Založ workspace, třeba `trading-agent`, a **nastav mu spend limit**, třeba 10 $ měsíčně. Kdyby klíč někdy unikl, nikdo s ním víc neutratí.
3. V tom workspace vytvoř API klíč.
4. Zadej ho:

   ```bash
   python -m trader keys        # vlož jen „Claude API key“, ostatní přeskoč Enterem
   ```

Agent má navíc vlastní strop `llm_budget_usd` (5 $ na běh).

## 3. Kontrola, přehrání posledních 48 hodin a týden na papíře (bez burzy, bez rizika)

```bash
python -m trader check --brain rules       # bezpečnostní kontrola + tabulka trhu z Bitvavo
```

Jak by agent obchodoval posledních 48 hodin a co mezitím reálně udělal trh:

```bash
python -m trader backtest --window-hours 48 --timeframe 15m                              # pravidla, zdarma
python -m trader backtest --window-hours 48 --timeframe 15m --brain claude --yes-llm-cost  # Claude, cca 1–2 $
```

Stáhne ze stejné burzy skutečné 15minutové svíčky: posledních 48 hodin plus 200 hodin historie předtím, aby měl agent z čeho počítat ukazatele. Agent pak obchoduje krok po kroku, v každém okamžiku jen s daty, která by tehdy znal. Na konci vypíše:

- obchody agenta,
- co skutečně udělal každý coin (změna, rozpětí, nejlepší možný obchod s dokonalou znalostí budoucnosti),
- výsledek „koupit a držet“,
- rozdíl mezi agentem a trhem.

Pak týden na papíře:

```bash
python -m trader run --name paper-week     # týden se skutečnými cenami, ale simulovanými obchody
```

Agent běží v terminálu. `Ctrl+C` ho zastaví. Stejný příkaz se stejným `--name` pokračuje tam, kde skončil. Průběžný stav uvidíš v druhém terminálu:

```bash
python -m trader report --name paper-week
```

Bez Claude klíče přidej `--brain rules`.

## 4. Burza Bitvavo (15–30 minut a čekání na ověření)

1. Zaregistruj se na [bitvavo.com](https://bitvavo.com) (Česko je podporovaná země), ověř identitu a **zapni 2FA přes aplikaci, ne přes SMS**.
2. Pošli na účet 10 € převodem SEPA.
3. Na webu (v aplikaci to nejde) otevři **Settings → API → Add new API key**:
   - **Permissions: jen „View“ a „Trade“. „Withdraw“ NEZAŠKRTÁVEJ.** Bitvavo upozorňuje, že výběry přes API nevyžadují 2FA ani potvrzení e-mailem. Klíč bez tohoto práva peníze z účtu poslat nemůže.
   - **IP whitelist:** zadej veřejnou IP adresu počítače, na kterém agent poběží, pokud je stálá. Ukradený klíč pak odjinud nefunguje. Pokud se ti IP mění, nech pole prázdné. Bez práva výběru je to pořád v pořádku.
   - Potvrď odkaz v e-mailu. **Secret se zobrazí jen jednou.**
4. Zadej klíče:

   ```bash
   python -m trader keys        # Enter u Claude klíče ho ponechá, vlož API key a API secret
   python -m trader check       # musí ukázat „API key OK, free EUR: 10.00“
   ```

## 5. Ostrý týden s 10 €

```bash
python -m trader run --live --name live-week1 --capital 10
```

Agent nejdřív udělá bezpečnostní kontrolu. Odmítne start, pokud je problém: `.env` v gitu, kapitál nad pojistkou 50 €. Pak se zeptá, jestli opravdu obchodovat. Napiš `YES`.

Počítač musí celý týden běžet a nesmí usnout, jinak agent nehlídá stop-lossy. Varianta přes Docker je v README v části „Kde to nechat běžet týden“.

## 6. Během týdne

- Stav: `python -m trader report --name live-week1`
- Podrobný log: `runs/live-week1/agent.log`
- **Nouzové zastavení:**
  1. `Ctrl+C` zastaví agenta, ale pozice nechá otevřené.
  2. `python -m trader close-all --live --name live-week1` prodá všechno a běh ukončí.

  Jde to i ručně v aplikaci Bitvavo.

## 7. Po týdnu

- `python -m trader report --name live-week1 --csv` vypíše souhrn a uloží křivku vývoje účtu do `equity.csv`.
- Pokud nechceš pokračovat, **smaž API klíč na Bitvavo i v Console**.
- K daním viz README, část „Zákony a daně v ČR“. Pro týden s 10 € se nic nepřiznává.
