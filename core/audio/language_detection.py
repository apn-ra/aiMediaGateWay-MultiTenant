# Author: RA
# Purpose: Nemo Language Detection
# Created: 28/09/2025

from nemo.collections.asr.models import EncDecSpeakerLabelModel

class NeMoLIDClient:
    def __init__(self, confidence_threshold=0.7):
        self.model = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.confidence_threshold = confidence_threshold

    def initialize(self):
        self.model = EncDecSpeakerLabelModel.from_pretrained("langid_93")
        self.model.to(self.device)
        self.model.eval()

    def detect_language(self, audio_data: np.ndarray, sample_rate: int = 16000):
        if audio_data.ndim > 1:
            audio_data = audio_data.mean(axis=1)

        # Resample to 16kHz if needed
        if sample_rate != 16000:
            audio_data = torchaudio.functional.resample(
                torch.tensor(audio_data), sample_rate, 16000
            ).numpy()
            sample_rate = 16000

        tensor = torch.tensor(audio_data).float().unsqueeze(0).to(self.device)
        length = torch.tensor([tensor.shape[1]]).to(self.device)

        with torch.no_grad():
            logits = self.model.forward(input_signal=tensor,
                                        input_signal_length=length)
            probs = torch.softmax(logits, dim=-1)
            idx = torch.argmax(probs, dim=-1).item()
            confidence = probs[0, idx].item()
            language = self.model.cfg.labels[idx]

        return {"language": language, "confidence": confidence}
