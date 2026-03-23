"""
Utilitaires pour le regroupement des notes de communication par conversation
et le nettoyage des aperçus email.
"""
import re
from collections import OrderedDict
from bs4 import BeautifulSoup


def get_conversation_counts_and_data(db_session, conv_ids, company_id):
    """
    Pre-calculate conversation counts and oldest/newest notes for a list of conversation IDs.

    Args:
        db_session: SQLAlchemy session (db.session)
        conv_ids: List of conversation_id strings
        company_id: The company ID to filter on

    Returns:
        tuple: (conversation_counts dict, conversation_data dict)
            conversation_counts: {conversation_id: count}
            conversation_data: {conversation_id: {'oldest': note, 'newest': note}}
    """
    from sqlalchemy import func, and_
    from models import CommunicationNote

    conversation_counts = {}
    conversation_data = {}

    if not conv_ids:
        return conversation_counts, conversation_data

    # Get counts
    counts = db_session.query(
        CommunicationNote.conversation_id,
        func.count(CommunicationNote.id)
    ).filter(
        CommunicationNote.conversation_id.in_(conv_ids),
        CommunicationNote.company_id == company_id
    ).group_by(CommunicationNote.conversation_id).all()
    conversation_counts = {cid: cnt for cid, cnt in counts}

    # Get oldest (original) message per conversation
    oldest_subq = db_session.query(
        CommunicationNote.conversation_id,
        func.min(CommunicationNote.created_at).label('min_date')
    ).filter(
        CommunicationNote.conversation_id.in_(conv_ids),
        CommunicationNote.company_id == company_id
    ).group_by(CommunicationNote.conversation_id).subquery()

    oldest_notes = db_session.query(CommunicationNote).join(
        oldest_subq, and_(
            CommunicationNote.conversation_id == oldest_subq.c.conversation_id,
            CommunicationNote.created_at == oldest_subq.c.min_date
        )
    ).filter(CommunicationNote.company_id == company_id).all()

    # Get newest message per conversation
    newest_subq = db_session.query(
        CommunicationNote.conversation_id,
        func.max(CommunicationNote.created_at).label('max_date')
    ).filter(
        CommunicationNote.conversation_id.in_(conv_ids),
        CommunicationNote.company_id == company_id
    ).group_by(CommunicationNote.conversation_id).subquery()

    newest_notes = db_session.query(CommunicationNote).join(
        newest_subq, and_(
            CommunicationNote.conversation_id == newest_subq.c.conversation_id,
            CommunicationNote.created_at == newest_subq.c.max_date
        )
    ).filter(CommunicationNote.company_id == company_id).all()

    oldest_map = {n.conversation_id: n for n in oldest_notes}
    newest_map = {n.conversation_id: n for n in newest_notes}

    for conv_id in conv_ids:
        if conv_id:
            conversation_data[conv_id] = {
                'oldest': oldest_map.get(conv_id),
                'newest': newest_map.get(conv_id)
            }

    return conversation_counts, conversation_data


def clean_email_preview(text, max_length=150):
    """
    Nettoie le texte d'un email HTML pour en faire un aperçu propre.
    Extrait le contenu textuel principal, supprime signatures et headers de réponse.
    
    Args:
        text: Le contenu HTML ou texte de l'email
        max_length: Longueur maximale de l'aperçu
        
    Returns:
        str: Texte nettoyé pour l'aperçu
    """
    if not text:
        return ""
    
    # Utiliser BeautifulSoup pour extraire le texte proprement
    try:
        soup = BeautifulSoup(text, 'html.parser')
        
        # Supprimer les éléments de style et script
        for element in soup(['style', 'script', 'head', 'meta', 'link']):
            element.decompose()
        
        # Supprimer les images (logos de signature)
        for img in soup.find_all('img'):
            img.decompose()
        
        # Extraire le texte
        raw_text = soup.get_text(separator='\n')
    except Exception:
        # Fallback si BeautifulSoup échoue
        raw_text = re.sub(r'<[^>]+>', '\n', text)
    
    # Nettoyer les lignes
    lines = raw_text.split('\n')
    clean_lines = []
    signature_started = False
    reply_header_started = False
    
    # Patterns de début de signature
    signature_starters = [
        r'(?i)^(cordialement|sincèrement|regards|best regards|bien à vous|salutations|merci|thanks)[\s,.:]*$',
        r'(?i)^(sent from|envoyé depuis|get outlook|obtenir outlook).*',
        r'^[-_=─━]{5,}',
    ]
    
    # Patterns de header de réponse (à partir de là, c'est le message précédent)
    reply_header_patterns = [
        r'(?i)^(de|from)\s*:\s*.+@',
        r'(?i)^(envoyé|sent)\s*:\s*\d',
        r'(?i)^(à|to)\s*:\s*.+@',
        r'(?i)^(objet|subject)\s*:\s*',
        r'(?i)^le\s+\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}.*a écrit',
        r'(?i)^on\s+\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}.*wrote',
    ]
    
    # Patterns à ignorer (lignes de signature seules)
    ignore_patterns = [
        r'^\+?[\d\s\-\(\)\.]{10,}$',  # Numéros de téléphone
        r'^https?://\S+$',  # URLs
        r'^www\.\S+$',
        r'^[\w\.\-]+@[\w\.\-]+\.\w+$',  # Emails seuls
    ]
    
    # Patterns de signature professionnelle (Nom + Titre avec séparateur |)
    professional_signature_patterns = [
        r'^[A-Z][a-zéèêëàâäùûüîïôöç]+ [A-Z][a-zéèêëàâäùûüîïôöç]+\s*\|',  # "Prénom Nom |"
        r'(?i).*(expert|spécialiste|directeur|manager|consultant|responsable|chef|ingénieur|analyste|coordonnateur|superviseur).*\|',
    ]
    
    for line in lines:
        line = line.strip()
        
        # Ignorer les lignes vides au début
        if not line and not clean_lines:
            continue
        
        # Détecter le début d'un header de réponse (message précédent)
        for pattern in reply_header_patterns:
            if re.match(pattern, line):
                reply_header_started = True
                break
        
        if reply_header_started:
            break
        
        # Détecter le début d'une signature
        for pattern in signature_starters:
            if re.match(pattern, line):
                signature_started = True
                break
        
        if signature_started:
            continue
        
        # Détecter une signature professionnelle (Nom + Titre | ...)
        for pattern in professional_signature_patterns:
            if re.match(pattern, line):
                signature_started = True
                break
        
        if signature_started:
            continue
        
        # Ignorer les lignes de signature isolées
        is_signature_line = False
        for pattern in ignore_patterns:
            if re.match(pattern, line):
                is_signature_line = True
                break
        
        if is_signature_line:
            continue
        
        # Ligne valide
        if line:
            clean_lines.append(line)
    
    # Joindre les lignes avec virgule pour un aperçu plus lisible
    result = ', '.join(clean_lines)
    
    # Nettoyer les espaces multiples et virgules doubles
    result = re.sub(r'\s+', ' ', result).strip()
    result = re.sub(r',\s*,', ',', result)
    result = re.sub(r'^,\s*', '', result)
    result = re.sub(r',\s*$', '', result)
    
    # Tronquer proprement
    if len(result) > max_length:
        truncated = result[:max_length]
        # Couper au dernier espace ou virgule pour éviter de couper un mot
        last_sep = max(truncated.rfind(' '), truncated.rfind(','))
        if last_sep > max_length // 2:
            truncated = truncated[:last_sep]
        result = truncated.rstrip(' ,') + '...'
    
    return result


def group_notes_by_conversation(notes, conversation_counts=None, conversation_data=None, load_children=False):
    """
    Regroupe les notes email par conversation_id.
    Les notes non-email restent séparées.
    
    LOGIQUE: Le message le PLUS RÉCENT est affiché en ligne principale.
    Le message ORIGINAL (le plus ancien) est dans les enfants avec un badge distinctif.
    
    IMPORTANT: Gère aussi les notes liées via parent_note_id (transferts avec sujet modifié)
    qui ont un conversation_id différent mais sont logiquement liées à la conversation parente.
    Remonte toute la chaîne parent_note_id pour fusionner les conversations qui partagent
    un ancêtre commun, même avec des conversation_id différents.
    
    CAS SPÉCIAL: Si une note a parent_note_id mais que ce parent n'est pas dans la page
    courante (pagination), une requête DB est effectuée pour trouver l'ancêtre racine.
    
    Args:
        notes: Liste de CommunicationNote triée par date décroissante
        conversation_counts: Dict optionnel {conversation_id: count} - nombre total de messages
        conversation_data: Dict optionnel {conversation_id: {'newest': note, 'oldest': note}}
                          contenant le plus récent et le plus ancien de chaque conversation
        load_children: Si True, peuple children avec toutes les notes de la conversation
                      (sauf le parent). Utilisé pour la page Détail Client.
                      Si False, children reste vide (chargement AJAX pour page Notes).
        
    Returns:
        list: Liste de dictionnaires représentant les groupes de notes
              Chaque groupe contient:
              - 'parent': Le message le PLUS RÉCENT de la conversation
              - 'children': Liste des autres messages (si load_children=True)
              - 'is_conversation': Boolean indiquant si c'est une conversation groupée
              - 'count': Nombre total de messages dans la conversation
              - 'original_date': Date du courriel original (le plus ancien)
              - 'original_id': ID du courriel original pour le badge
              - 'has_linked_parent': Boolean si la note a un parent_note_id hors page (pour forcer is_conversation)
    """
    if conversation_counts is None:
        conversation_counts = {}
    if conversation_data is None:
        conversation_data = {}
    
    conversations = OrderedDict()
    standalone_notes = []
    seen_canonical_keys = set()
    notes_already_grouped = set()  # Notes déjà rattachées via parent_note_id
    
    # Créer un index par ID pour recherche rapide
    notes_by_id = {n.id: n for n in notes}
    
    # Cache pour les ancêtres récupérés de la DB (évite requêtes multiples)
    db_ancestor_cache = {}
    
    def get_ancestor_from_db(note_id):
        """Récupère l'ancêtre racine depuis la DB quand hors page."""
        if note_id in db_ancestor_cache:
            return db_ancestor_cache[note_id]
        
        try:
            from app import db
            from models import CommunicationNote
            
            # Requête récursive pour trouver l'ancêtre racine
            current_id = note_id
            visited = set()
            root_conv_id = None
            
            while current_id and current_id not in visited:
                visited.add(current_id)
                ancestor = db.session.query(
                    CommunicationNote.parent_note_id,
                    CommunicationNote.conversation_id
                ).filter(CommunicationNote.id == current_id).first()
                
                if not ancestor:
                    break
                
                if ancestor.conversation_id:
                    root_conv_id = ancestor.conversation_id
                
                if not ancestor.parent_note_id:
                    break
                
                current_id = ancestor.parent_note_id
            
            db_ancestor_cache[note_id] = root_conv_id
            return root_conv_id
        except Exception:
            return None
    
    def find_root_ancestor(note):
        """
        Remonte la chaîne parent_note_id pour trouver l'ancêtre racine.
        Si un ancêtre est hors page, utilise la DB pour continuer la remontée.
        Retourne le conversation_id de l'ancêtre racine.
        """
        current = note
        best_conv_id = note.conversation_id
        visited = {note.id}
        
        # Phase 1: Remonter dans la page courante
        while current.parent_note_id and current.parent_note_id in notes_by_id:
            if current.parent_note_id in visited:
                break  # Cycle détecté
            visited.add(current.parent_note_id)
            current = notes_by_id[current.parent_note_id]
            if current.conversation_id:
                best_conv_id = current.conversation_id
        
        # Phase 2: Si le parent est hors page, continuer via DB
        if current.parent_note_id and current.parent_note_id not in notes_by_id:
            db_conv_id = get_ancestor_from_db(current.parent_note_id)
            if db_conv_id:
                best_conv_id = db_conv_id
        
        return best_conv_id
    
    # Calculer la clé canonique (conversation_id de l'ancêtre racine) pour chaque note
    canonical_key_map = {}  # note.id -> canonical_conversation_id
    for note in notes:
        if note.note_type == 'email' and note.conversation_id:
            canonical_key_map[note.id] = find_root_ancestor(note)
    
    # Identifier les notes liées via parent_note_id DANS la page courante
    # Map: canonical_conv_id -> list of linked notes (transferts avec sujet modifié)
    linked_by_canonical = {}
    for note in notes:
        if note.note_type == 'email' and note.conversation_id:
            canonical_key = canonical_key_map.get(note.id, note.conversation_id)
            # Si la clé canonique est différente du conversation_id de la note,
            # cette note doit être fusionnée dans le groupe canonique
            if canonical_key != note.conversation_id:
                if canonical_key not in linked_by_canonical:
                    linked_by_canonical[canonical_key] = []
                linked_by_canonical[canonical_key].append(note)
                notes_already_grouped.add(note.id)
    
    # Identifier les notes qui ont un parent_note_id HORS de la page courante
    # Ces notes font partie d'une conversation plus large qu'on ne peut pas voir
    notes_with_external_parent = set()
    for note in notes:
        if note.parent_note_id and note.parent_note_id not in notes_by_id:
            # Le parent existe mais n'est pas dans cette page - c'est une note liée externe
            notes_with_external_parent.add(note.id)
    
    # Indexer toutes les notes par clé canonique (au lieu de conversation_id)
    notes_by_canonical = {}
    for note in notes:
        if note.note_type == 'email' and note.conversation_id:
            canonical_key = canonical_key_map.get(note.id, note.conversation_id)
            if canonical_key not in notes_by_canonical:
                notes_by_canonical[canonical_key] = []
            notes_by_canonical[canonical_key].append(note)
    
    for note in notes:
        # Ignorer les notes déjà rattachées à une autre conversation via parent_note_id
        if note.id in notes_already_grouped:
            continue
            
        if note.note_type != 'email' or not note.conversation_id:
            standalone_notes.append({
                'parent': note,
                'children': [],
                'is_conversation': False,
                'count': 1,
                'original_date': None,
                'original_id': None
            })
            continue
        
        # Utiliser la clé canonique (conversation_id de l'ancêtre racine)
        canonical_key = canonical_key_map.get(note.id, note.conversation_id)
        
        if canonical_key in seen_canonical_keys:
            continue
        
        seen_canonical_keys.add(canonical_key)
        
        # Vérifier si une note du groupe a un parent externe (hors page)
        has_external_parent = any(n.id in notes_with_external_parent for n in notes_by_canonical.get(canonical_key, []))
        
        # Récupérer toutes les notes de ce groupe canonique
        all_conv_notes = list(notes_by_canonical.get(canonical_key, []))
        
        # RÈGLE CRITIQUE: Le parent (newest_note) doit TOUJOURS venir des notes
        # réellement dans ce groupe canonique. Ne JAMAIS laisser conversation_data
        # (indexé par conversation_id brut) surcharger le parent, car un même
        # conversation_id peut être partagé entre deux groupes canoniques différents.
        # Si conversation_data tire une note d'un AUTRE groupe, les deux groupes
        # auraient le même parent → même ID d'accordéon → collision.
        if all_conv_notes:
            all_conv_notes.sort(key=lambda x: x.created_at, reverse=True)
            newest_note = all_conv_notes[0]
            oldest_note = sorted(all_conv_notes, key=lambda x: x.created_at)[0]
        else:
            newest_note = note
            oldest_note = None
        
        # Pour la date originale uniquement (informationnel), on peut utiliser
        # conversation_data SEULEMENT pour oldest (jamais pour newest/parent).
        # Et seulement si le conv_id appartient exclusivement à CE groupe canonique.
        if not load_children and oldest_note:
            for cid in set(n.conversation_id for n in all_conv_notes if n.conversation_id):
                conv_info = conversation_data.get(cid, {})
                if conv_info.get('oldest'):
                    if conv_info['oldest'].created_at < oldest_note.created_at:
                        oldest_note = conv_info['oldest']
        
        # Peupler children si demandé
        children = []
        if load_children:
            children = [n for n in all_conv_notes if n.id != newest_note.id]
            children.sort(key=lambda x: x.created_at, reverse=True)
        
        total_notes_count = len(all_conv_notes)
        
        # Calculer le nombre à afficher.
        # total_notes_count = notes réellement dans ce groupe canonique (fiable).
        # Pour les vues paginées (load_children=False), vérifier si la DB indique
        # plus de notes que celles chargées, en utilisant UNIQUEMENT le conversation_id
        # du canonical_key lui-même (pas les conv_ids partagés avec d'autres groupes).
        if not load_children:
            own_sql_count = conversation_counts.get(canonical_key, 0)
            display_count = max(total_notes_count, own_sql_count, 2 if has_external_parent else 1)
            is_conversation = display_count > 1 or has_external_parent
        else:
            display_count = max(total_notes_count, 2 if has_external_parent else 1)
            is_conversation = total_notes_count > 1 or has_external_parent
        
        conversations[canonical_key] = {
            'parent': newest_note,
            'children': children,
            'is_conversation': is_conversation,
            'count': display_count,
            'has_external_parent': has_external_parent,
            'original_date': oldest_note.created_at if oldest_note else None,
            'original_id': oldest_note.id if oldest_note else None,
            'canonical_key': canonical_key,
            '_sort_date': newest_note.created_at
        }
    
    all_groups = list(conversations.values()) + standalone_notes
    
    def get_sort_date(group):
        return group.get('_sort_date', group['parent'].created_at)
    
    all_groups.sort(key=get_sort_date, reverse=True)
    
    for group in all_groups:
        group.pop('_sort_date', None)
    
    return all_groups


def get_conversation_summary(group):
    """
    Génère un résumé pour une conversation groupée.
    
    Args:
        group: Dictionnaire contenant parent, children, is_conversation, count
        
    Returns:
        dict: Informations de résumé (sujet, participants, dernière activité)
    """
    parent = group['parent']
    children = group.get('children', [])
    
    # Collecter tous les participants
    participants = set()
    if parent.email_from:
        participants.add(parent.email_from.split('<')[0].strip())
    if parent.email_to:
        for email in parent.email_to.split(';'):
            participants.add(email.split('<')[0].strip())
    
    for child in children:
        if child.email_from:
            participants.add(child.email_from.split('<')[0].strip())
    
    # Dernière activité
    if children:
        last_activity = children[0].created_at
    else:
        last_activity = parent.created_at
    
    return {
        'subject': parent.email_subject or 'Sans sujet',
        'participants': list(participants)[:3],  # Max 3 participants affichés
        'last_activity': last_activity,
        'total_messages': group['count']
    }
