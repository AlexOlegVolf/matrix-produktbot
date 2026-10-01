# Warunung: Ich habe das von einer LLM schreiben lassen


## 1. Repository und Python

Ein eigenes öffentliches GitHub-Repository mit ausschließlich den Dateien dieses Pakets anlegen. Kein bestehendes Docker-Datenvolume oder vertrauliche Workflow-Exporte veröffentlichen. Eine feste geprüfte Version auschecken, nicht bei jedem Dienststart automatisch `git pull` ausführen.

Als Administrator, wenn das Dienstkonto noch nicht existiert:

```bash
sudo useradd --system --home-dir /var/lib/produktbot --shell /usr/sbin/nologin produktbot
sudo git clone https://github.com/DEIN-KONTO/DEIN-REPOSITORY.git /opt/produktbot
sudo python3.12 -m venv /opt/produktbot/.venv
sudo /opt/produktbot/.venv/bin/python -m pip install -r /opt/produktbot/requirements.txt
cd /opt/produktbot
sudo /opt/produktbot/.venv/bin/python -m unittest discover -s tests -v
```

Der Programmordner bleibt im Besitz der Administration; der Dienst muss ihn nur lesen können. Schreibzugriff braucht er nur auf seinen Datenordner. Ist die Serverarchitektur ohne passende Python-Wheels, können für `aiohttp` Build-Werkzeuge nötig sein; Paketinstallation vor dem Umzug prüfen.

## 2. Einstellungen und zwei Geheimnisse

```bash
sudo install -d -o root -g produktbot -m 0750 /etc/produktbot
sudo install -o root -g produktbot -m 0640 /opt/produktbot/deploy/config.env.example /etc/produktbot/config.env
sudo install -o root -g produktbot -m 0640 /dev/null /etc/produktbot/matrix-token.secret
sudo install -o root -g produktbot -m 0640 /dev/null /etc/produktbot/webhook-key.secret
```

Die beiden `install ... /dev/null`-Befehle sind **nur für die Ersteinrichtung**. Bei erneuter Ausführung würden sie vorhandene Schlüsseldateien leeren.

Mit dem geschützten Editor der Administration die Dateien befüllen:

- `config.env`: technische HTTPS-Serveradresse, vollständige Bot-ID, erlaubter Matrix-Servername und n8n-Production-URL ersetzen. Homeserver-Adresse und ID-Domain können unterschiedlich sein.
- `matrix-token.secret`: nur den vollständigen Access-Token des Bot-Kontos, ohne `Bearer`.
- `webhook-key.secret`: nur den vereinbarten Wert für `X-Bot-Key`; identisch zum n8n-Header-Auth-Credential.

Die Dateien bleiben außerhalb des Git-Repositories. Keine Zugangsdaten in Tickets, Screenshots oder Shell-Befehle kopieren, die im Verlauf landen.

Der Bot unterstützt auch `--matrix-token WERT` und `--webhook-key WERT`, falls die IT dieses bestehende Parameterverfahren ausdrücklich verwenden möchte. Diese Werte können jedoch in Prozessinformationen sichtbar werden. Die Dienstvorlage übergibt deshalb **Dateipfade als Parameter**; Geheimnisse selbst stehen nicht in der Prozessargumentliste.

## 3. Dienst einrichten und starten

```bash
sudo install -m 0644 /opt/produktbot/deploy/produktbot.service /etc/systemd/system/produktbot.service
sudo systemd-analyze verify /etc/systemd/system/produktbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now produktbot
sudo systemctl status produktbot --no-pager
sudo journalctl -u produktbot -n 50 --no-pager
```

Erwartet: „Bot angemeldet“ und beim ersten Empfang „Empfang bereit“. Jetzt von einem Mitarbeiterkonto eine **neue** Suchnachricht senden. Ein bestehender verschlüsselter Raum ist ungeeignet; privaten unverschlüsselten Chat gemäß Firmenkonfiguration verwenden. Der Bot nimmt eine Einladung an, wenn danach genau Bot und Mitarbeiter beigetreten sind und keine weiteren Personen eingeladen sind.

Der Ordner `/var/lib/produktbot` wird von systemd angelegt. Er enthält `state.sqlite3`, gegebenenfalls WAL-Dateien und die Prozesssperre. **Bei Updates oder Neustarts nicht löschen.** Nur eine Instanz für dieses Bot-Konto betreiben; alten Docker-Bot und alte n8n-Matrix-Abfrage deaktivieren.

## 4. Betrieb

```bash
# Protokoll live ansehen; Strg+C beendet nur die Ansicht
sudo journalctl -u produktbot -f

# Nach einer Konfigurationsänderung neu starten
sudo systemctl restart produktbot

# Anhalten
sudo systemctl stop produktbot

# Nach behobenem Fehler und ausgeschöpfter Startbegrenzung
sudo systemctl reset-failed produktbot
sudo systemctl start produktbot
```

Bei geändertem Unit-File zusätzlich `systemctl daemon-reload`. Ein abgelaufener/widerrufener Matrix-Token wird nicht automatisch erneuert. IT ersetzt die Token-Datei, startet den Dienst neu und testet eine neue Anfrage. Bei Tokenwechsel können serverseitige Transaktions-Dublettenregeln anders greifen; offene Zustellungen prüfen.

Bei einer Netzstörung läuft der Empfang mit Wiederholungen weiter. Bei ungültiger Matrix-Anmeldung oder nicht auflösbarer Nachrichtenlücke stoppt der Prozess. systemd versucht begrenzt neu zu starten. Dienststatus und wiederholte Fehler müssen in die vorhandene Überwachung aufgenommen werden.

## 5. Update, Backup und Rücknahme

1. Dienst stoppen.
2. Datenordner vollständig und geschützt sichern (bei gestopptem Dienst einschließlich vorhandener SQLite-Nebendateien); aktuellen Git-Commit notieren.
3. Geprüfte neue Git-Version auschecken, Abhängigkeiten installieren, Tests ausführen.
4. Dienst starten und Abnahme mit zwei Mitarbeitern wiederholen.
5. Bei Problemen Dienst stoppen, vorherige Programmversion wiederherstellen. Bei künftig geänderter Datenbankstruktur ist gegebenenfalls auch das passende Backup erforderlich.

Keine parallelen Instanzen mit kopierter Datenbank starten. Die lokale Dateisperre verhindert nur Doppelstarts mit **demselben** Datenordner. Ein zweiter Host oder anderer Datenordner wird davon nicht erfasst.

## Häufige Ursachen

| Meldung / Verhalten | Prüfen |
|---|---|
| Matrix-Zugang ungültig | Richtiger Bot-Token, nicht abgemeldet/widerrufen; Serveradresse korrekt. |
| Token gehört nicht zum Bot-Konto | Bot-ID und Inhaber des Tokens passen nicht zusammen. |
| n8n HTTP 401/403 | X-Bot-Key auf beiden Seiten identisch. |
| n8n HTTP 404 | Production-URL richtig und Workflow veröffentlicht? |
| n8n HTTP 500 | Fehlgeschlagene n8n-Ausführung öffnen, insbesondere LeanConnect-Zugang prüfen. |
| Antwort kein gültiges JSON / falsche Anfrage | `Respond to Webhook` nach N8N.md konfigurieren. |
| Raum wird nicht bearbeitet | Privat, unverschlüsselt, genau zwei beigetretene Nutzer, richtige Mitarbeiter-Domain; Protokoll prüfen. |
| Neueinladung bleibt unbeantwortet | Einladender muss Mitarbeiter der freigegebenen Domain sein. Bei zuvor abgelehntem Raum neuen privaten Raum verwenden. |
| Nach Erstinstallation keine Antwort auf alten Text | Nach „Empfang bereit“ erneut schreiben. |
| Nachrichtenlücke | Server-Historie, Zugriffsrechte und längere Ausfallzeit prüfen. Datenbank nicht blind zurücksetzen. |

Quelle zum Dienstbetrieb: [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html).
