# Min feed

Henter nyheder, subreddits og YouTube-kanaler, skjuler opslag med ord fra
din blokliste og bygger en side, som GitHub Pages hoster. Siden opdateres hver halve time.

## Kom i gang

1. Opret et **offentligt** repository på GitHub (Pages fra private repos kræver betalt plan).
2. Upload alle filerne, inklusive mappen `.github`.
3. Gå til **Settings > Pages** og vælg **GitHub Actions** som kilde.
4. Gå til fanen **Actions**, vælg "Byg feed" og tryk **Run workflow**.
5. Når kørslen er grøn, ligger siden på `https://DIT-BRUGERNAVN.github.io/REPO-NAVN/`.

## Ændringer

Alt ændres i `config.yaml`: kilder, kategorier, blokerede ord og hvor gamle opslag må være.
Hver ændring, du pusher, bygger siden igen.

## Test lokalt

    pip install -r requirements.txt
    python build.py

Åbn derefter `site/index.html` i browseren. Loggen viser, hvor mange opslag
hver kilde gav, og hvilke der fejlede.

## Godt at vide

- GitHub sætter planlagte kørsler på pause, hvis der ikke har været aktivitet i repoet i 60 dage.
  Du får en mail og kan slå dem til igen med ét klik.
- Fejler en kilde, bygges siden alligevel, og kilden nævnes øverst på siden.
