"""
tug_logging.py -- Centrale logging-configuratie voor de TUG-ontheffingen-pipeline.

Bevat:
- setup_logging(): configureert de root-logger éénmalig bij pipeline-start.
- LogAccumulator: drop-in vervanger voor de bestaande nested `log()`-closures.
  Routeert berichten naar de Python `logging`-module én accumuleert ze in
  een lijst voor latere weergave in het PDF-proceslog.

Niveau-detectie op basis van tekstpatronen:
  'FOUT' / 'MISLUKT' / 'CONFLICT' / 'NIET TOEGESTAAN' → ERROR
  'WAARSCHUWING' / '✗'                                → WARNING
  alle overige berichten                              → INFO

De bestaande `WAARSCHUWING:`-prefix in de berichten blijft bewaard zodat het
PDF-proceslog en de stdout-output ongewijzigd ogen voor de gebruiker.
"""

import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """Configureer de root-logger één keer bij pipeline-start.

    Het format is bewust minimalistisch (alleen het bericht) om de visuele
    output identiek te houden aan de eerdere `print()`-stijl. Wie het log
    naar een bestand wil dirigeren of niveau wil filteren, kan dit via
    standaard `logging.basicConfig`-aanpassingen of een eigen handler.
    """
    root = logging.getLogger()
    # Voorkom dat tests / herhaalde imports dubbele handlers toevoegen.
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(level)


class LogAccumulator:
    """Callable die berichten doorstuurt naar `logging` én accumuleert.

    Vervangt het patroon:

        log_regels = []
        def log(tekst):
            print(tekst)
            log_regels.append(tekst)

    door:

        log = LogAccumulator(__name__)
        ...
        state["validatie"]["log_regels"] = log.lines
    """

    def __init__(self, logger_name: str) -> None:
        self.logger = logging.getLogger(logger_name)
        self.lines: list[str] = []

    def __call__(self, tekst: str) -> None:
        self.lines.append(tekst)
        self._log_at_appropriate_level(tekst)

    def _log_at_appropriate_level(self, tekst: str) -> None:
        stripped = tekst.strip()
        if any(m in stripped for m in ("FOUT", "MISLUKT", "CONFLICT", "NIET TOEGESTAAN")):
            self.logger.error(tekst)
        elif "WAARSCHUWING" in stripped or "✗" in stripped:
            self.logger.warning(tekst)
        else:
            self.logger.info(tekst)
