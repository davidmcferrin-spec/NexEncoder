"""The DeckLink patch has to land on the FFmpeg 9.0.2 demuxer, and only once."""

from nexencoder.decklink_patch import MARKER, patch_decklink_source

SOURCE = """\
#include "decklink_dec.h"
static uint8_t *get_metadata(AVFormatContext *avctx, uint16_t *buf, size_t width,
                             uint8_t *tgt, size_t tgt_size, AVPacket *pkt)
{
        } else if (did == 0x61 && sdid == 0x01) {
            clear_parity_bits(buf, len);
            data = vanc_to_cc(avctx, buf, width, data_len);
            if (data) {
                if (av_packet_add_side_data(pkt, AV_PKT_DATA_A53_CC, data, data_len) < 0)
                    av_free(data);
            }
        } else {
            av_log(avctx, AV_LOG_DEBUG, "Unknown meta data DID = 0x%.2x SDID = 0x%.2x\\n",
                    did, sdid);
        }
}
"""


def test_patch_inserts_scte104_and_caption_sends():
    patched = patch_decklink_source(SOURCE)
    assert patched.count(MARKER) == 3
    assert "did == 0x41 && sdid == 0x07" in patched
    assert "nexencoder_vanc_send(avctx, pkt, 1, buf + 6, len - 7)" in patched
    assert "nexencoder_vanc_send(avctx, pkt, 2, buf + 6, len - 7)" in patched
    assert "nexencoder VANC forwarder" in patched
    assert patched.index(MARKER) < patched.index("static uint8_t *get_metadata(")
    assert patch_decklink_source(patched) == patched


def test_patch_rejects_an_unexpected_demuxer():
    try:
        patch_decklink_source("int main(void) { return 0; }\n")
    except ValueError as exc:
        assert "VANC metadata" in str(exc)
    else:
        raise AssertionError("expected ValueError")
