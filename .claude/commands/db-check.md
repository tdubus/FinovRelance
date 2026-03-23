---
name: db-check
description: >
  Verification de l'etat de la base de donnees. Utiliser pour verifier la connectivite,
  compter les tables et enregistrements, et s'assurer que les tables critiques existent.
---

Verifie l'etat de la base de donnees :
1. Lis DATABASE_URL depuis les variables d'environnement
2. Connecte-toi et compte le nombre de tables dans le schema public
3. Compte le nombre d'utilisateurs dans la table "users"
4. Verifie que les tables critiques existent : users, clients, invoices, campaigns, companies
5. Affiche un rapport avec les compteurs
