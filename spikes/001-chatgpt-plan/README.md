# 001 — ChatGPT plan usage, niezależny test k90

**Verdict: PARTIAL — tester sprawdzony lokalnie; logowanie i inference na koncie
użytkownika wymagają jeszcze rzeczywistego uruchomienia.** Nie jest to integracja
produkcyjna. Kod k90, konfiguracja kontenera, SQLite i Hindsight pozostają bez zmian.

## Pytanie

Czy konto z subskrypcją pozwoli wykonać rozmowę, wywołanie lokalnego narzędzia
oraz analizę obrazu bez użycia płatnego klucza API?

Test używa oficjalnego OAuth Sign in with ChatGPT z uprawnieniem do wykorzystania
planu i publicznego Responses API. Nie kopiuje poświadczeń Codex, HS ani Hermesa.

## Uruchomienie na Macu

Wymagane: Git, Python 3.10+ i dostęp do przeglądarki oraz Internetu. Nie uruchamiaj
przez SSH/`hsh`: callback `127.0.0.1` musi trafić do procesu na tym samym Macu,
na którym otwierasz przeglądarkę.

Z osobnego katalogu roboczego:

```sh
git clone --depth 1 --branch spike/chatgpt-subscription https://github.com/hermesinski/k90.git k90-subscription-test && sh k90-subscription-test/spikes/001-chatgpt-plan/run.sh
```

Jeżeli masz już tę kopię, kolejne uruchomienie wymaga tylko:

```sh
sh k90-subscription-test/spikes/001-chatgpt-plan/run.sh
```

Runner tworzy osobne środowisko Python w `~/.cache/k90-subscription-probe/venv`.
Nie zmienia systemowych bibliotek, środowiska k90 ani Hermesa.

1. W przeglądarce zaloguj się na **swoje konto z subskrypcją** i zatwierdź
   aplikację **k90 Subscription Test**, w tym zgodę na wykorzystanie planu.
2. Tester zweryfikuje podpis ID tokenu, issuer, audience, nonce i przyznany zakres.
   Wyświetli listę modeli faktycznie dostępną dla tego konta; wybierz model.
3. W ustawieniach użycia ChatGPT dla tej aplikacji **wyłącz korzystanie z dodatkowych
   kredytów**. Tester nie odczytuje ani nie zmienia tego ustawienia i sam brak
   klucza API nie gwarantuje niewykorzystania kredytów konta. Potwierdź w terminalu
   dopiero po sprawdzeniu tej opcji. Jeżeli nie możesz jej znaleźć lub wyłączyć,
   przerwij test, zamiast zgadywać.
4. Tester wykona maksymalnie cztery żądania modelowe: tekst, wywołanie narzędzia,
   odpowiedź po wyniku narzędzia i rozpoznanie syntetycznego obrazka.
5. Dostaniesz raport `~/.config/k90-subscription-probe/result.json` bez tokenów,
   e-maila, identyfikatora konta i surowych odpowiedzi. Ten raport można przekazać
   do analizy. Nie wysyłaj pliku `registration.json` ani adresu callbacku.

Obraz to czerwone koło i niebieski kwadrat; narzędzie zwraca dwa jawnie fikcyjne
posiłki. Test nie otwiera rzeczywistych baz ani zdjęć. Rozpoznanie tego obrazka
nie dowodzi jakości analizy zdjęć posiłków ani poprawności porad zdrowotnych.

## Bezpieczeństwo i ograniczenia

- OAuth: PKCE S256, losowe state/nonce, callback wyłącznie na IPv4 loopback,
  weryfikacja podpisu RS256 przez klucze OpenAI i zgodności konta przy ponownym logowaniu.
- Zwrócony access token nie jest zastępowany kluczem z `.env` lub środowiska.
  Nie ma płatnego fallbacku, automatycznego kupowania kredytów ani retry inference.
- Tokeny istnieją tylko w pamięci procesu. Lokalny plik metadanych rejestracji
  zawiera client ID, identyfikator hosta i subject, ale nie tokeny; ma tryb `600`,
  a katalog `700`. Metadane nie są anonimowe i nie są przeznaczone do publikacji.
- Po zakończeniu lub błędzie tester próbuje unieważnić odnawialną sesję.
  Jeżeli raport nie potwierdza revocation, odłącz aplikację w ustawieniach ChatGPT.
  Przymusowe zabicie procesu może uniemożliwić ten krok; zdalne odłączenie jest wtedy konieczne.
- Brak stałego magazynu tokenów i testu odświeżania oznacza, że nie jest to gotowy
  mechanizm pracy bezobsługowej. Taką obsługę trzeba zaprojektować przed integracją.
- Endpointy są ograniczone do jawnej listy OpenAI; transport nie podąża za redirectami.
  Pomyślny test wymaga `response.completed`, nie tylko HTTP 200 czy częściowego tekstu.
- Raport `VALIDATED` potwierdza tylko trzy próby na wybranym modelu i koncie w tym
  momencie; nie daje gwarancji przyszłych limitów ani pełnej zgodności k90.
- Nie zmniejszaj zabezpieczeń konta i nie przenoś tokenów przez czat.

## Co sprawdzono lokalnie

- Testy offline: callback na prawdziwym lokalnym serwerze z fikcyjnym dostawcą,
  podpisane fikcyjne ID tokeny, błędne state/audience/nonce, brak scope, przerwany
  i błędny SSE, ograniczenie endpointów, brak fallbacku oraz przebieg trzech prób.
  Wyników atrap nie traktujemy jako wykonanych zapytań do modelu.
- Preflight rzeczywistej sieci: odczytano publiczne metadane OIDC i klucze
  podpisujące OpenAI. Nie wymagał logowania i nie testował subskrypcji.

```sh
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s . -p checks.py -v
.venv/bin/python probe.py --preflight
```

## Co dalej po udanym teście

Osobny adapter k90, własne bezpieczne poświadczenia, odświeżanie tokenów,
obsługa limitów i przerwanych odpowiedzi, zgodność istniejących narzędzi i zdjęć,
oddzielna ścieżka summary oraz jawny wybór API/subskrypcja. Bez automatycznego
przełączenia na płatne API. Wdrożenie i dane produkcyjne wymagają osobnej zgody.

## Źródła kontraktu

- [Rejestracja i OAuth](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Modele i inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Ograniczenia preview](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [Sesje i wykorzystanie planu](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [Namespaces narzędzi](https://developers.openai.com/api/docs/guides/function-calling#defining-namespaces)

Nie jest wymagany DevKit Electron/React; tester realizuje opisany publiczny
protokół w samodzielnym procesie Python.
