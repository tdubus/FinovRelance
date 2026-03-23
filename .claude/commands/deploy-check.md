---
name: deploy-check
description: >
  Verification pre-deploiement du projet. Utiliser avant chaque push ou deploiement pour
  s'assurer qu'aucun secret n'est hardcode, que le .env n'est pas dans le staging git,
  que les dependances sont a jour, et que le build Docker passe.
---

Verifie que le projet est pret pour un deploiement :
1. Scanne le code pour des secrets hardcodes (sk_live_, postgresql://, etc.)
2. Verifie que .env n'est pas dans le staging git
3. Verifie que requirements.txt est a jour vs les imports
4. Verifie que le build Docker passe
5. Genere un rapport OK / PROBLEMES TROUVES
