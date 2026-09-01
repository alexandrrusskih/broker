# Security

This service stores OAuth refresh tokens and should be deployed only to a
dedicated Firebase project controlled by its operator. Do not reuse a Firebase
project that serves unrelated applications or Firestore clients.

Never commit broker keys, provider credentials, token files, Firebase service
account files, `.env` files, or operational infrastructure inventories. Local
configuration belongs in `~/.config/hltm-broker/config.json` with mode `0600`.

Report vulnerabilities through GitHub private vulnerability reporting for this
repository. Do not include live credentials in an issue, log, or reproduction.
