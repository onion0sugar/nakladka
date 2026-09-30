# MSSQL → ntfy

Bot sprawdza bazę MSSQL i wysyła powiadomienia do aplikacji ntfy.

## Uruchomienie

### 1. Zainstaluj wymagania

Potrzebujesz:

- Python 3.12 lub nowszy,
- Microsoft ODBC Driver 18 for SQL Server,
- dostępu do bazy MSSQL,
- serwerze ntfy dostępnym z komputera bota i telefonów Android.

### 2. Przygotuj środowisko

W katalogu projektu wykonaj:

```bash
python -m venv .venv
```

Aktywuj środowisko:

```bash
# Linux / macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Zainstaluj biblioteki:

```bash
pip install -r requirements.txt
```

### 3. Uzupełnij konfigurację

Skopiuj plik konfiguracyjny:

```bash
# Linux / macOS
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

Otwórz `.env` i wpisz prawidłowe dane:

```env
MSSQL_SERVER=adres-serwera
MSSQL_PORT=1433
MSSQL_DATABASE=nazwa-bazy
MSSQL_USERNAME=login
MSSQL_PASSWORD=haslo

NTFY_SERVER=http://192.168.24.90:8093
SUPERVISOR_TOPIC=supervisor
RESPONSE_TOPIC=order-responses
```

`SUPERVISOR_TOPIC` odbiera komunikaty nadzorcze. Bot nasłuchuje odmów na wspólnym
`RESPONSE_TOPIC`; aplikacja podaje ten topic w treści oferty. Topic każdego
pracownika to jego login MSSQL z `users.txt`. Polecenia testowe wysyłają
wiadomości na `SUPERVISOR_TOPIC`; można ustawić opcjonalny `TEST_TOPIC`, aby
kierować je na osobny topic.

### 4. Przygotuj pliki projektu

Dostosuj zapytania `query22.sql` i `query22_users.sql` do swojej bazy. Pierwsze
 jest głównym źródłem dokumentów typu 7 i 22, a drugie wskazuje użytkownika
odpowiedzialnego za spakowane pozycje.

Skopiuj plik użytkowników i wpisz jeden login w każdej linii:

```bash
cp users.txt.example users.txt
```

Każdy użytkownik ma jeden topic równy jego loginowi MSSQL. Nowe zamówienie jest
wysyłane z priorytetem `high`, a gotowe zamówienie z priorytetem `max`.
Nowe zamówienia trafiają pojedynczo do dostępnych użytkowników z `users.txt`,
którzy zmodyfikowali dziś co najmniej jeden dokument (`work_today_users.sql`).
Bot czeka 15 sekund na ofertę, wysyła następną po 16 sekundach i pomija osoby,
które mają już aktywną nakładkę lub zamówienie w toku. Supervisor dostaje
informację o nowym zamówieniu raz, a potem wynik próby: odmowę, wygaśnięcie
oferty albo przyjęcie potwierdzone statusem `in_progress` w MSSQL.
Wyjątkiem są powiadomienia „Gotowe do wydania”: użytkownik z `users.txt`, który
ma takie zamówienie, otrzyma je również bez wpisu w `work_today_users.sql`.
Supervisor dostaje informację o każdym zamówieniu „Gotowe do wydania” przy
każdym poprawnym pollingu, jeśli takie zamówienie istnieje. Jeśli w tym samym
pollingu wykryto gotowe zamówienie, zwykła oferta nowego zamówienia nie jest
wysyłana. Lista z `users.txt` jest wczytywana tylko przy starcie.

### 5. Sprawdź konfigurację

```bash
python main.py --test-db
python main.py --test-ntfy
python main.py --test-new
python main.py --test-ready
python main.py --test-notification moj-topic max
```

`--test-notification` przyjmuje topic i priorytet. Dozwolone priorytety to
`min`, `low`, `default`, `high`, `max` oraz wartości liczbowe `1`–`5`.

Pełną pętlę dispatchera można uruchomić na osobnej liście topiców testowych,
bez wysyłania ofert do `users.txt`:

```powershell
Copy-Item testusers.txt.example testusers.txt
python main.py --test-users
```

W `testusers.txt` wpisz jeden topic ntfy w każdym wierszu. Testowi odbiorcy są
traktowani jako aktywni i dostępni dla wszystkich aktualnych grup. Pętla nadal
odczytuje prawdziwe zamówienia z MSSQL, więc ich numery i linki trafią na topiki
testowe. Stan dispatchera, topic odpowiedzi i topic nadzorczy są oddzielone od
produkcji (`~/.local/state/nakladka/testusers.db`, `TEST_RESPONSE_TOPIC`,
`TEST_SUPERVISOR_TOPIC`). Proces pozostaje uruchomiony do `Ctrl+C`.

Jeśli testy zakończą się poprawnie, uruchom bota:

```bash
python main.py
```

Zatrzymanie programu: `Ctrl+C`.

Po każdej zmianie w `.env`, `users.txt` lub plikach `.sql` uruchom program ponownie.

## Uruchomienie serwera ntfy w Dockerze

Na serwerze uruchom serwer ntfy:

```bash
docker compose up -d
```

Sprawdzenie działania:

```bash
docker compose ps
docker compose logs -f ntfy
```

Telefon korzysta z adresu ustawionego w `NTFY_BASE_URL` w `docker-compose.yml`.
Dane serwera ntfy są przechowywane w katalogu `ntfy-data`.

Bot działa poza Dockerem jako usługa systemd z pliku `ntfy-bot.service`.

## Aplikacja Android

Projekt Android znajduje się w `../appka`. Każde urządzenie konfiguruje jeden
topic pracownika, adres serwera ntfy i opcjonalny token. Nie trzeba instalować
aplikacji ntfy na telefonie. Użytkownik przyznaje aplikacji dostęp do nakładek
oraz uruchamia nasłuch z ekranu startowego. Ustawienia są chronione hasłem.

W PowerShell z katalogu `appka` zbuduj APK:

```powershell
.\gradlew.bat assembleDebug
```

APK debug znajdziesz w `app\build\outputs\apk\debug\app-debug.apk`.
Usługa foreground pokazuje ciche powiadomienie wymagane przez Androida; opcja
konfiguracji ogranicza jego widoczność, ale Android nadal może pokazać aktywną
usługę w systemowym menedżerze.
