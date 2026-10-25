# NHL – szanse zawodników na gol, asystę, punkt i strzały

Skrypt pobiera mecze NHL z wybranego dnia i oblicza dla każdego zawodnika z pola (bez bramkarzy):

- **Gol** – szansa na co najmniej 1 gola
- **Asysta** – szansa na co najmniej 1 asystę
- **Punkt (G+A)** – szansa na co najmniej 1 punkt, a także na 2 lub więcej
- **Strzały** – oczekiwana liczba strzałów celnych oraz szanse na 2+, 3+ i 4+

Przy każdej szansie podany jest **kurs fair** (1 / szansa), który można porównać z kursem bukmachera.

Dane pochodzą z oficjalnego, darmowego API NHL (to samo źródło, z którego korzysta Yahoo). Klucz API nie jest potrzebny.

Osobna strona **Bonusy ELC** (przycisk w prawym górnym rogu raportu) pokazuje młodych zawodników na kontraktach wejściowych i szansę, że do końca sezonu osiągną progi bonusów „A”. Opis jest w sekcji „Bonusy ELC” niżej.

## Bonusy ELC

Dla każdego zawodnika model symuluje 10 000 razy resztę sezonu. Bierze pod uwagę liczbę pozostałych meczów drużyny, dotychczasowy udział zawodnika w meczach, jego tempo G/A (z niepewnością) i oczekiwany czas gry. Wynik to szansa na każdy próg statystyczny:

| | Gole | Asysty | Punkty | PKT na mecz (min. 42 mecze) |
|---|---|---|---|---|
| Napastnik | 20 | 35 | 60 | 0,73 |
| Obrońca | 10 | 25 | 40 | 0,49 |

Dodatkowo strona pokazuje bieżące miejsce zawodnika w drużynie pod względem czasu gry (próg: top 6 napastników albo top 4 obrońców) i w +/- (top 3). Po kliknięciu **Szczegóły** widać, ile zawodnikowi brakuje do każdego progu, jakie tempo na mecz jest do tego potrzebne i jakie tempo ma obecnie.

**Kto jest na ELC?** API NHL nie zawiera kontraktów, dlatego są dwa źródła:

- **kandydaci** są wybierani automatycznie: wiek do 24 lat i najwyżej 2 wcześniejsze sezony z co najmniej 10 meczami w NHL. To przybliżenie, więc kontrakt sprawdź na PuckPedii.
- **z listy** to zawodnicy wpisani przez ciebie do pliku **`elc.csv`** w repozytorium. Tylko dla nich strona zna kwotę bonusu, pokazuje oczekiwaną wartość w $ i działa filtr „Bonus ≥ 150k $”.

### Jak uzupełnić elc.csv (w przeglądarce)

1. W repozytorium kliknij plik **elc.csv**, a potem ikonę ołówka (**Edit this file**).
2. Pod nagłówkiem dopisuj zawodników, jeden w linii. Imię i nazwisko podaj tak, jak w raporcie, a kwotę bonusu za jeden próg weź z PuckPedii:
   ```
   zawodnik;bonus_za_prog_usd;uwagi
   Jan Kowalski;150000;
   Piotr Nowak;$212,500;
   Adam Zieliński;150k;ostatni rok ELC
   ```
   Kwotę można zapisać jako `150000`, `$150,000` albo `150k`. Kolumna uwag jest opcjonalna.
3. Kliknij **Commit changes**. Zmiana pojawi się na stronie przy następnym przebiegu automatu albo od razu po ręcznym **Run workflow**.

Zawodnik z listy, który nie jest w tej chwili w składzie drużyny NHL (np. został zesłany do AHL), nie pojawi się na stronie, dopóki nie wróci do składu.

Czego model nie liczy: progów przyznawanych w głosowaniu (All-Rookie Team, All-Star, nagrody) ani bonusów „B” (top 10 ligi).

## Wersja online (GitHub Pages) – zalecana

Strona sama aktualizuje się codziennie przez GitHub Actions. Nic nie trzeba instalować.

1. Na github.com utwórz nowe **publiczne** repozytorium, np. `nhl-model`. GitHub Pages za darmo działa tylko dla repozytoriów publicznych.
2. Wgraj **całą zawartość** tego folderu (**Add file → Upload files**, przeciągnij wszystko). Folder `.github` jest ukryty, więc w Windows włącz pokazywanie ukrytych plików albo utwórz plik ręcznie: **Add file → Create new file**, nazwa `.github/workflows/raport.yml`, i wklej treść.
3. W **Settings → Pages** ustaw: Source = **Deploy from a branch**, Branch = **main**, folder **/docs**, i kliknij Save.
4. W **Settings → Actions → General → Workflow permissions** zaznacz **Read and write permissions** i kliknij Save.
5. W zakładce **Actions** wybierz „Raport NHL”, kliknij **Run workflow** i zostaw pole daty puste. Pierwszy przebieg trwa 5–15 minut, następne około minuty.
6. Strona będzie pod adresem `https://TWOJA-NAZWA.github.io/nhl-model/`.

Od tej chwili raport aktualizuje się automatycznie **dwa razy dziennie**: około 13:17 i 22:17 czasu polskiego. Wieczorna aktualizacja wyłapuje zmiany w składach. Godziny zmienisz w `.github/workflows/raport.yml`; podaje się je w czasie UTC.

Na stronie:

- przełącznik **dnia** w prawym górnym rogu pokazuje archiwum wszystkich poprzednich raportów,
- przycisk **CSV** pobiera dane do Excela,
- raport dla dowolnej daty wygenerujesz przez **Run workflow**, wpisując datę w polu.

Jeśli przebieg w Actions skończy się błędem, otwórz go, skopiuj czerwony komunikat i wklej mi go.

## Wersja lokalna – instalacja (raz)

1. Zainstaluj Pythona 3.10 lub nowszego z python.org. Przy instalacji zaznacz **„Add Python to PATH”**.
2. Rozpakuj folder, otwórz w nim wiersz poleceń i wpisz:
   ```
   pip install -r requirements.txt
   ```

## Codzienne użycie

Dwuklik na **`uruchom.bat`** albo w wierszu poleceń:

```
python nhl_model.py                     # mecze z dzisiejszej daty (data polska)
python nhl_model.py --date 2026-10-25   # konkretny dzień
```

Raport otworzy się w przeglądarce. Pliki trafiają do folderu `raporty`:

- `nhl_RRRR-MM-DD.html` – interaktywna tabela z zakładkami Gole / Asysty / Punkty / Strzały, filtrami meczu, drużyny i pozycji, progiem minimalnej szansy oraz sortowaniem po kliknięciu w nagłówek
- `nhl_RRRR-MM-DD.csv` – te same dane do Excela (separator `;`, przecinek dziesiętny)

**Uwaga do daty:** mecze NHL zaczynają się wieczorem i w nocy czasu polskiego. Uruchomienie 25.10 o 10:00 pokaże mecze z 25.10 (amerykańskiej daty), czyli te, które zaczną się wieczorem 25.10 i w nocy na 26.10.

Pierwsze uruchomienie trwa kilka minut, bo pobiera historię około 300–500 zawodników z 4 sezonów. Kolejne uruchomienia są szybkie: zakończone sezony zapisują się na stałe w folderze `cache`, a bieżący odświeża się co 6 godzin.

## Automatyczne uruchamianie (Windows)

1. Otwórz **Harmonogram zadań**, a w nim wybierz **Utwórz zadanie podstawowe**.
2. Ustaw wyzwalacz **Codziennie**, np. o 18:00.
3. Jako akcję wybierz **Uruchom program** i wskaż `uruchom.bat`. W polu „Rozpocznij w” podaj folder z modelem.

## Jak liczony jest model

Liczba goli, asyst czy strzałów zawodnika w meczu jest modelowana rozkładem Poissona. Oczekiwana liczba zdarzeń λ to iloczyn pięciu składników:

| Składnik | Jak liczony |
|---|---|
| Tempo na 60 min | bieżący sezon oraz 3 poprzednie z wagami 1,00 / 0,70 / 0,45 / 0,30; ostatnie 10 meczów ×1,5; wygładzanie 250 „wirtualnymi” minutami średniego gracza na tej pozycji, żeby mała próba nie dawała absurdów |
| Czas na lodzie | 60% średnia z ostatnich 10 meczów, 40% średnia ważona |
| Obrona rywala | gole tracone (dla G/A/PKT) lub strzały dopuszczane (dla SOG) na tle średniej ligi; na początku sezonu mieszane z poprzednim sezonem |
| Dom / wyjazd | ×1,03 / ×0,97 |
| H2H | wyniki zawodnika przeciw temu rywalowi z bieżącego i 5 poprzednich sezonów w porównaniu z tym, czego model by oczekiwał; mocno wygładzone i ograniczone do 0,80–1,25, bo to zwykle tylko kilka meczów |

Szansa na co najmniej jedno zdarzenie wynosi P(≥1) = 1 − e^(−λ).

Wszystkie parametry są na górze pliku `nhl_model.py` (sekcja „parametry modelu”), więc możesz je zmieniać.

## Sprawdź, czy model trafia (backtest)

```
python nhl_model.py --backtest 2025-11-01 2026-03-31
```

Backtest przelicza model dla każdego dnia z zakresu, używając wyłącznie danych sprzed tego dnia, i porównuje prognozy z rzeczywistymi wynikami. Wyświetla dwie rzeczy:

- **Brier i „Poprawa”**: o ile model jest lepszy niż zgadywanie średniej. Wartość powyżej 0% oznacza, że model wnosi wartość.
- **Kalibrację**: czy prognozy „30%” rzeczywiście sprawdzają się w około 30% przypadków.

Zrób to przed postawieniem prawdziwych pieniędzy na podstawie modelu. Zastrzeżenie: liczba strzałów dopuszczanych przez rywala pochodzi z całego sezonu, więc w backteście ta jedna składowa zna trochę „przyszłości”.

## Ograniczenia

- **Składy i kontuzje.** Model nie zna składu na konkretny mecz. Zawodnicy, którzy opuścili 2 lub więcej ostatnich meczów drużyny, są oznaczani i domyślnie ukrywani. Mimo to sprawdź oficjalne składy i linie przed meczem.
- **Bramkarz rywala.** Model nie wie, kto stanie w bramce. Gdy gra rezerwowy, szanse strzeleckie zwykle rosną.
- **Linie i przewagi.** Awans do 1. linii albo 1. formacji przewagi szybko zmienia szanse, a model wychwytuje to dopiero po kilku meczach (przez czas na lodzie).
- **Sezon zasadniczy.** Liczony jest tylko sezon zasadniczy; mecze play-off nie trafiają do historii.
- **Szacunki, nie pewniki.** Nawet najlepszy zawodnik strzela gola w mniej niż połowie meczów.
