"""The DeckLink muxer patch writes captions and SCTE-104, and only once."""

from nexencoder.sdi_patch import patch_decklink_muxer

SOURCE = """\
    if (ctx->supports_vanc && ctx->dlo->EnableVideoOutput(ctx->bmd_mode, bmdVideoOutputVANC) != S_OK) {
        av_log(avctx, AV_LOG_WARNING, "Could not enable video output with VANC! Trying without...\\n");
        ctx->supports_vanc = 0;
    }
    if (!ctx->supports_vanc && ctx->dlo->EnableVideoOutput(ctx->bmd_mode, bmdVideoOutputFlagDefault) != S_OK) {
        av_log(avctx, AV_LOG_ERROR, "Could not enable video output!\\n");
        return -1;
    }

static int decklink_write_video_packet(AVFormatContext *avctx, AVPacket *pkt)
{
        hr = ctx->dlo->ScheduleVideoFrame(frame,
"""


def test_patch_enables_vanc_and_attaches_ancillary():
    patched = patch_decklink_muxer(SOURCE)
    assert patched.count("static void nexencoder_sdi_anc(") == 1
    assert patched.count("nexencoder_sdi_anc(avctx, ctx, pkt, frame, st);") == 1
    assert "nexencoder SDI ancillary" in patched
    assert "nexencoder_sdi_anc(avctx, ctx, pkt, frame, st);" in patched
    assert patched.index("static void nexencoder_sdi_anc(") < patched.index("static int decklink_write_video_packet(")
    assert patched.index("nexencoder_sdi_anc(avctx, ctx, pkt, frame, st);") < patched.index(
        "ScheduleVideoFrame(frame,"
    )
    enable = patched.index("EnableVideoOutput(ctx->bmd_mode, bmdVideoOutputVANC)")
    assert "supports_vanc &&" not in patched[enable - 80:enable]
    assert "0x61" in patched and "0x41" in patched
    assert "GetBufferForVerticalBlankingLine(11" in patched
    assert "GetBufferForVerticalBlankingLine(13" in patched
    assert patch_decklink_muxer(patched) == patched


def test_patch_rejects_a_muxer_without_the_anchors():
    try:
        patch_decklink_muxer("int main(void) { return 0; }\n")
    except ValueError as exc:
        assert "decklink_enc.cpp" in str(exc)
    else:
        raise AssertionError("expected the patch to reject an unrelated file")
