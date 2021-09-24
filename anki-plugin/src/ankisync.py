"""
ankisync.py - sync TiddlyWiki notes to Anki

After finding all notes in a user's TiddlyWikis, we need to sync with Anki.
This is a unidirectional, destructive sync -- all TiddlyWiki notes found are
compared with all Anki notes in the collection, and the Anki collection is
updated to match, by adding and updating notes to match those found in the
set of TiddlyWiki notes, then deleting any notes in the Anki collection that
use TiddlyRemember models and were not found in that set. Any changes made in
Anki and not in TiddlyWiki will be lost at this point.

The sync() method is the public interface to this module.
"""
from datetime import datetime
from typing import Any, Dict, Set, cast

import anki.consts
from anki.notes import Note

from . import trmodels
from .twnote import TwNote
from .util import pluralize, Twid


def _change_note_type(col: Any, tw_note: TwNote, anki_note: Note) -> Note:
    """
    If the ID is now a cloze note rather than a question note or vice versa,
    change the note type in Anki prior to trying to complete the sync.
    Return the updated note.
    """
    old_model_name = anki_note.model()['name']  # type: ignore
    old_model_definition = trmodels.by_name(old_model_name)
    assert old_model_definition is not None, \
        f"A note of a type TiddlyRemember does not support ('{old_model_name}') " \
        f"was found. TiddlyRemember does not know how to fix this note. " \
        f"This is probably TiddlyRemember's fault -- please consider reporting " \
        f"this error. "

    fmap = old_model_definition.field_remap(tw_note.model)
    cmap = old_model_definition.card_remap(tw_note.model)
    new_model = col.models.by_name(tw_note.model.name)
    col.models.change(anki_note.note_type(), [anki_note.id], new_model, fmap, cmap)

    return col.getNote(col.find_notes(f"nid:{anki_note.id}")[0])


def _set_initial_scheduling(tw_note: TwNote, anki_note: Note, col: Any):
    """
    When a note is added, apply any starting scheduling information supplied by
    the TiddlyWiki note. Subsequent syncs will keep Anki's scheduling information,
    unless the note is deleted and then synced back again.

    Currently, the same scheduling must be applied to all cards of a note.

    Not sure whether this is the best-practice way to set initial scheduling:
    https://forums.ankiweb.net/t/correct-way-to-apply-arbitrary-scheduling-changes-to-a-card/13638
    """
    if tw_note.schedule is not None:
        for cid in col.find_cards(f"nid:{anki_note.id}"):
            c = col.get_card(cid)

            c.queue = anki.consts.QUEUE_TYPE_REV
            c.type = anki.consts.CARD_TYPE_REV
            c.ivl = tw_note.schedule.ivl
            c.factor = tw_note.schedule.ease
            c.lapses = tw_note.schedule.lapses
            col.update_card(c)

            days_from_today = (tw_note.schedule.due - datetime.now().date()).days
            col.sched.set_due_date((c.id,), str(days_from_today))


def _update_deck(tw_note: TwNote, anki_note: Note, col: Any, default_deck: str) -> None:
    """
    Given a note already in Anki's database, move its cards into an
    appropriate deck if they aren't already there. All cards must go to the
    same deck for the time being.

    The note must be flushed to Anki's database for this to work correctly.
    """
    # Confusingly, col.decks.id returns the ID of an existing deck, and
    # creates it if it doesn't exist. This happens to be exactly what we want.
    deck_name = tw_note.target_deck or default_deck
    new_did = col.decks.id(deck_name)
    for card in anki_note.cards():
        if card.did != new_did:
            card.did = new_did
            card.flush()


def sync(tw_notes: Set[TwNote], col: Any, default_deck: str) -> str:
    """
    Compare TiddlyWiki notes with the notes currently in our Anki collection
    and add, edit, and remove notes as needed to get Anki in sync with the
    TiddlyWiki notes.

    :param twnotes: Set of TwNotes extracted from a TiddlyWiki.
    :param col: The Anki collection object.
    :return: A log string to pass back to the user, describing the results.

    .. warning::
        This is a unidirectional update. Any changes to notes made directly
        in Anki WILL BE LOST when the sync is run.

    The sync keys on unique IDs, which are generated by the TiddlyWiki
    plugin when you add macro calls and default to the current UTC time
    in YYYYMMDDhhmmssxxx format (xxx being milliseconds). Provided the ID
    is maintained, integrity is retained when the macro is edited and
    moved around to different tiddlers. Altering the ID in either Anki or
    TiddlyWiki will break the connection and likely cause a duplicate
    note (or, worse, an overwritten note if you reuse an existing ID).

    Be aware that deleting a note from TiddlyWiki will permanently delete
    it from Anki.
    """
    # Make sure the note types exist and haven't been modified in a way
    # that could prevent the sync from working properly.
    trmodels.ensure_note_types(col)
    trmodels.verify_note_types(col)

    # Retrieve Anki notes and TiddlyWiki notes and identify what adds, edits,
    # and removes are needed to update the Anki collection.
    extracted_notes: Set[TwNote] = tw_notes
    extracted_twids: Set[Twid] = set(n.id_ for n in extracted_notes)
    extracted_notes_map: Dict[Twid, TwNote] = {n.id_: n for n in extracted_notes}

    model_search = ' or '.join(f'note:"{i.name}"' for i in trmodels.all_note_types())
    anki_notes: Set[Note] = set(col.getNote(nid)
                                for nid in col.find_notes(model_search))
    id_field = trmodels.ID_FIELD_NAME
    anki_twids: Set[Twid] = set(cast(Twid, n[id_field]) for n in anki_notes)
    anki_notes_map: Dict[Twid, Note] = {cast(Twid, n[id_field]): n for n in anki_notes}

    adds = extracted_twids.difference(anki_twids)
    edits = extracted_twids.intersection(anki_twids)
    removes = anki_twids.difference(extracted_twids)

    userlog = []

    # Make the changes to the collection.
    for note_id in adds:
        tw_note = extracted_notes_map[note_id]
        n = Note(col, col.models.byName(tw_note.model.name))
        tw_note.update_fields(n)
        deck = col.decks.id(tw_note.target_deck or default_deck)
        col.add_note(n, deck)
        _set_initial_scheduling(tw_note, n, col)

    userlog.append(f"Added {len(adds)} {pluralize('note', len(adds))}.")

    edit_count = 0
    for note_id in edits:
        anki_note = anki_notes_map[note_id]
        tw_note = extracted_notes_map[note_id]
        if not tw_note.model_equal(anki_note):
            new_note = _change_note_type(col, tw_note, anki_note)
            anki_note = anki_notes_map[note_id] = new_note
        if not tw_note.fields_equal(anki_note):
            tw_note.update_fields(anki_note)
            anki_note.flush()
            edit_count += 1
        _update_deck(tw_note, anki_note, col, default_deck)
    userlog.append(f"Updated {edit_count} {pluralize('note', edit_count)}.")

    col.remove_notes([anki_notes_map[twid].id for twid in removes])
    userlog.append(f"Removed {len(removes)} {pluralize('note', len(removes))}.")

    return '\n'.join(userlog)
