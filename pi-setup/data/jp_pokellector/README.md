# Japanese Pokellector data drop

Drop CSVs scraped by [Ngansen/PokeScraper_3.0](https://github.com/Ngansen/PokeScraper_3.0)
into this folder. The Flask POS bind-mounts the directory into the container
at `/app/data/jp_pokellector`, and the `import_jpn_cards.py` importer picks
up everything matching `*.csv`.

## Recommended flow

1. On a dev machine with Chrome installed:

   ```bash
   git clone https://github.com/Ngansen/PokeScraper_3.0
   cd PokeScraper_3.0
   pip install -r requirements.txt   # selenium, pandas, etc.
   python 0_Poke_Sets_V2.py          # writes pokellector_set_data.csv
   python 01_Pokellector_V3.py       # writes per-card CSVs
   ```

2. Copy the resulting CSVs to the Pi:

   ```bash
   scp data/*.csv pi@192.168.86.36:~/Hanryx-Vault-POS/pi-setup/data/jp_pokellector/
   ```

3. Trigger an import on the Pi:

   ```bash
   curl -u admin:hanryxvault -X POST http://192.168.86.36:8080/admin/jpn-cards/refresh
   ```

The importer is column-name-fuzzy — it recognises `URL`, `Set`, `Name`,
`CardNumber`, `Rarity`, `Image`, `JapaneseName`, etc., regardless of capitalisation.

## Future option

Run PokeScraper periodically as a sidecar Docker service with `selenium/standalone-chrome`.
That's a heavier add — bring it on once you've validated the data quality
manually first.
