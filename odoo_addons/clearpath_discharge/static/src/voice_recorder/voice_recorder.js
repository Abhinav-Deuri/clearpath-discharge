/** @odoo-module **/
// Doctor voice note field: record from the microphone OR pick any audio file.
// Audio is decoded in the browser and stored as 16 kHz mono 16-bit WAV, the format the
// sidecar's Riva (Parakeet) speech-to-text expects.
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, proxy, useProps, xml, t } from "@odoo/owl";

const TARGET_RATE = 16000;
const MAX_SECONDS = 180;

async function toWav16k(arrayBuffer) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    let decoded;
    try {
        decoded = await ctx.decodeAudioData(arrayBuffer);
    } finally {
        ctx.close();
    }
    const frames = Math.ceil(decoded.duration * TARGET_RATE);
    const offline = new OfflineAudioContext(1, frames, TARGET_RATE);
    const src = offline.createBufferSource();
    src.buffer = decoded;
    src.connect(offline.destination);
    src.start();
    const rendered = await offline.startRendering();
    const pcm = rendered.getChannelData(0);
    const out = new DataView(new ArrayBuffer(44 + pcm.length * 2));
    const str = (o, s) => [...s].forEach((c, i) => out.setUint8(o + i, c.charCodeAt(0)));
    str(0, "RIFF");
    out.setUint32(4, 36 + pcm.length * 2, true);
    str(8, "WAVEfmt ");
    out.setUint32(16, 16, true);
    out.setUint16(20, 1, true); // PCM
    out.setUint16(22, 1, true); // mono
    out.setUint32(24, TARGET_RATE, true);
    out.setUint32(28, TARGET_RATE * 2, true);
    out.setUint16(32, 2, true);
    out.setUint16(34, 16, true);
    str(36, "data");
    out.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) {
        const v = Math.max(-1, Math.min(1, pcm[i]));
        out.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
    }
    return out.buffer;
}

function toBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(bin);
}

export class VoiceRecorderField extends Component {
    static template = xml`
        <div class="o_clearpath_voice d-flex flex-column gap-2">
            <div class="d-flex flex-wrap align-items-center gap-2">
                <button type="button" class="btn btn-light border" t-if="!this.state.recording"
                        t-on-click="this.startRecording" t-att-disabled="this.props.readonly or this.state.busy">
                    <i class="oi text-danger me-2" data-icon="mic"/>Record voice note
                </button>
                <button type="button" class="btn btn-light border" t-if="this.state.recording" t-on-click="this.stopRecording">
                    <i class="oi text-danger me-2 cp-beat-fade" data-icon="stop"/>Stop (<t t-out="this.state.seconds"/>s)
                </button>
                <label class="btn btn-light border mb-0" t-if="!this.state.recording and !this.props.readonly">
                    Upload audio file
                    <input type="file" accept="audio/*" class="d-none" t-on-change="this.onFile"/>
                </label>
                <button type="button" class="btn btn-link" t-if="this.hasAudio and !this.props.readonly and !this.state.recording"
                        t-on-click="this.clear">Remove</button>
                <span class="o_cp_wave" t-if="this.state.recording" title="Recording"><i/><i/><i/><i/><i/><i/><i/></span>
                <span class="text-muted" t-if="this.state.busy">Converting&#8230;</span>
            </div>
            <audio controls="controls" t-if="this.state.previewUrl" t-att-src="this.state.previewUrl" style="max-width:360px"/>
            <span class="text-muted" t-if="this.hasAudio and !this.state.previewUrl">Voice note saved (WAV).</span>
            <span class="text-danger" t-if="this.state.error" t-out="this.state.error"/>
        </div>`;
    props = useProps({
        ...standardFieldProps,
        fileNameField: t.string().optional(),
    });

    setup() {
        this.state = proxy({
            recording: false, busy: false, seconds: 0, previewUrl: "", error: "",
        });
        this.chunks = [];
        this.recorder = null;
        this.timer = null;
    }

    get hasAudio() {
        return !!this.props.record.data[this.props.name];
    }

    async startRecording() {
        this.state.error = "";
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            this.state.error = "Microphone not available (needs http://localhost or HTTPS).";
            return;
        }
        let stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        } catch {
            this.state.error = "Microphone permission denied.";
            return;
        }
        this.chunks = [];
        this.recorder = new MediaRecorder(stream);
        this.recorder.ondataavailable = (ev) => ev.data.size && this.chunks.push(ev.data);
        this.recorder.onstop = async () => {
            stream.getTracks().forEach((tr) => tr.stop());
            await this.save(new Blob(this.chunks, { type: this.recorder.mimeType }), "dictation.wav");
        };
        this.recorder.start();
        this.state.recording = true;
        this.state.seconds = 0;
        this.timer = setInterval(() => {
            this.state.seconds += 1;
            if (this.state.seconds >= MAX_SECONDS) {
                this.stopRecording();
            }
        }, 1000);
    }

    stopRecording() {
        clearInterval(this.timer);
        this.state.recording = false;
        if (this.recorder && this.recorder.state !== "inactive") {
            this.recorder.stop();
        }
    }

    async onFile(ev) {
        const file = ev.target.files[0];
        ev.target.value = "";
        if (file) {
            await this.save(file, (file.name || "voice_note").replace(/\.[^.]+$/, "") + ".wav");
        }
    }

    async save(blob, filename) {
        this.state.busy = true;
        this.state.error = "";
        try {
            const wav = await toWav16k(await blob.arrayBuffer());
            if (wav.byteLength > 9.5 * 1024 * 1024) {
                throw new Error("Recording is too long (max about 5 minutes).");
            }
            const changes = { [this.props.name]: { filename, content: toBase64(wav) } };
            if (this.props.fileNameField && this.props.fileNameField in this.props.record.fields) {
                changes[this.props.fileNameField] = filename;
            }
            await this.props.record.update(changes);
            if (this.state.previewUrl) {
                URL.revokeObjectURL(this.state.previewUrl);
            }
            this.state.previewUrl = URL.createObjectURL(new Blob([wav], { type: "audio/wav" }));
        } catch (e) {
            this.state.error = "Could not process audio: " + (e.message || e);
        } finally {
            this.state.busy = false;
        }
    }

    async clear() {
        this.state.previewUrl = "";
        await this.props.record.update({ [this.props.name]: false });
    }
}

registry.category("fields").add("wav_recorder", {
    component: VoiceRecorderField,
    displayName: "Voice recorder",
    supportedTypes: ["binary"],
    extractProps: ({ attrs }) => ({ fileNameField: attrs.filename }),
});
