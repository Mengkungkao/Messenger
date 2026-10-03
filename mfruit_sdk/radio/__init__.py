"""The shared LoRa radio: one setup, one identity, one contact list.

Every MFruit app that uses the LoRa HAT (WalkieTalkie, Messenger, ...)
talks through the same module on the same frequency, so the things they
must agree on are stored once, in MFruit OS's shared radio directory
(``<MFruit OS home>/shared/radio``, mode 0700), instead of in each app:

    radio.json      module settings written by MFruit OS's radio setup
                    (frequency, air rate, power, port); apps only read it
    device.json     this radio's Device ID and name
    keys.json       this radio's keys and the keys of every paired radio
    contacts.json   names of the paired radios

Pairing in one app therefore works in every app. ``settings`` needs only
the standard library; ``keyring`` and ``crypto`` need the ``cryptography``
package (installed by the radio setup), so import them only in radio apps.

    from mfruit_sdk.radio import settings
    radio = settings.load_radio()          # None until the setup has run
    device = settings.load_device()        # Device ID, assigned once
    from mfruit_sdk.radio.keyring import Keyring
    keyring = Keyring()                     # shared keys, created on first use
"""
