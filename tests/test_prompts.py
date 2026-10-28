from devdash.prompts import readable


def test_plain_prompt_is_unchanged():
    assert readable("add a docstring to hello") == "add a docstring to hello"


def test_pulls_the_users_words_out_of_app_markup():
    wrapped = """<wake reason="mention">
  <project id="p"><thread ts="t">
    <message trigger="true" from="human" trust="principal" id="m">make the button &amp; label bigger</message>
  </thread></project>
  <system-note>harness text</system-note>
</wake>"""
    assert readable(wrapped) == "make the button & label bigger"


def test_prefers_cited_user_text_then_falls_back_to_the_note():
    relay = """<relay from="coordinator">
  <cited id="c1" author="user" name="V">ok fix the readme</cited>
  <note>Vardaan wants a README fix.</note>
</relay>"""
    assert readable(relay) == "ok fix the readme"
    assert readable('<relay from="coordinator"><note>Make one small edit.</note></relay>') == "Make one small edit."


def test_attached_files_and_harness_notes_are_left_out():
    wrapped = '''@"/home/v/.claude/uploads/a.png" <wake reason="message-edited">
  <message trigger="true" from="human" id="m">why is money being spent</message>
  <system-note>The owner edited the message named above.</system-note>
</wake>'''
    assert readable(wrapped) == "why is money being spent"
    assert readable('@"/tmp/a.png" fix this') == "fix this"


def test_the_message_that_woke_the_session_wins_over_quoted_ones():
    wake = """<wake reason="mention">
  <message from="human" id="old">earlier message</message>
  <message trigger="true" from="human" id="new">the new request</message>
</wake>"""
    assert readable(wake) == "the new request"


def test_a_background_task_notification_is_not_shown_as_a_request():
    note = "<task-notification>\n<task-id>b1</task-id>\n<summary>Background command done</summary>\n</task-notification>"
    assert readable(note) == "(follow-up after a background task finished)"
