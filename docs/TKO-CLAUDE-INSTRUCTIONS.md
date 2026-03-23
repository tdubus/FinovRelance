# TKO — Instructions pour CLAUDE.md

Copier les sections pertinentes dans le CLAUDE.md de chaque projet.

---

## Section à ajouter dans CLAUDE.md

```markdown
## TKO — Token Killer Optimized

TKO est installé globalement. Il compresse automatiquement les outputs
des commandes shell (git, cargo, docker, npm, grep, etc.) via un hook
PreToolUse. Aucune action requise pour les commandes Bash.

### Cache Read

TKO cache les lectures de fichiers. Quand tu relis un fichier inchangé,
tu reçois :

    [fichier inchangé — contenu identique à lecture #N]

Ce message signifie que le contenu est strictement identique à ta lecture #N.
Le cache se réinitialise automatiquement après 5 lectures consécutives.

### Compaction du contexte

Quand le contexte est compacté, TKO le détecte automatiquement et :
1. Purge tout le cache de lecture
2. Injecte ce message dans ton contexte :

       [TKO] Compaction detected — read cache has been reset.
       All file reads will return full content (no cache).
       Files that were cached : [liste]

Quand tu vois ce message, tous les fichiers que tu avais lus
précédemment ne sont plus en cache. Si tu as besoin du contenu
d'un fichier pour continuer ton travail, tu dois le relire —
la lecture retournera le contenu complet (pas de cache).

### Orientation projet

Si PROJECT_INDEX.md existe à la racine, le lire en début de session
avant toute exploration du codebase. Il contient la structure du projet,
les exports principaux, les routes API, les variables d'environnement
et les dépendances. Ne pas lancer grep pour s'orienter si l'index
est disponible.

Pour régénérer l'index : `tko index`

### Commandes TKO utiles

- `tko read --aggressive <fichier>` — affiche uniquement les signatures
  (fn, struct, class, interface, exports) sans les corps de fonctions.
  Utiliser quand tu as besoin de la structure d'un fichier sans le détail.

- `tko smart <fichier>` — résumé heuristique en 2 lignes : type de module,
  nombre de fonctions publiques, types définis, dépendances principales.
  Utiliser pour évaluer rapidement un fichier avant de le lire en entier.

- `tko gain` — affiche les statistiques de compression de la session.
```

---

## Notes d'installation

Pour que ces instructions soient actives :

1. Copier la section ci-dessus dans le `CLAUDE.md` à la racine du projet
2. S'assurer que `tko` est dans le PATH (`tko --version`)
3. S'assurer que les hooks sont installés (`tko init --global`)
4. Générer l'index si souhaité (`tko index`)
