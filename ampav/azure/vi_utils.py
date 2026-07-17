#!/bin/env python3.12
import io
from typing import Any
from ampav.core.schema.audio import AudioEffectType, AudioEffects, AudioEffectSegment
from ampav.core.schema.object import DetectedObject, DetectedObjects
from ampav.core.schema.transcript import Transcript
from ampav.core.schema.video import VideoPattern, VideoPatternType, VideoPatterns
from ampav.core.utils import hhmmss2seconds
from ampav.core.schema.compound import CompoundOutput
from ampav.core.schema.segments import ParagraphSegment, WordSegment
from ampav.core.schema.tool import ToolOutput
from ampav.core.schema.image import Image

def parse_vi_data(native: dict):
    tool_output = ToolOutput(tool_name="Azure Video Indexer",
                                tool_version="1.0",
                                output=CompoundOutput())


    # if the data looks like it's a viraw that we're generating internally
    # we'll use that, otherwise we'll make the raw data (which probably came
    # directly from AVI) look like what we're generating.
    if native.get('format', None) != 'viraw' and 'data' not in native.keys():
        print("Wrapping!")
        native = {'data': native,
                    'thumbnails': {}}
    print(list(native['data'].keys()))
    # Load the speaker names.    
    video = native['data']['videos'][0]
    speakers = {}
    for speaker in video['insights']['speakers']:
        speakers[speaker['id']] = speaker['name']
    
    # Let's get the transcript first.  AVI gives us the audio in paragraph
    # style chunks. We'll populate that and then create the rest of the
    # transcript types from that.        
    transcript = Transcript(media_duration=hhmmss2seconds(video['insights']['duration']),
                            languages=video['insights']['languages'])
            
    for para in video['insights']['transcript']:
        # I don't know if AVI ever returns more than one instace per
        # paragraph, but just to be sure, I'm going to iterate. Also,
        # I'm using adjusted{Start,End} because that's guaranteed to be
        # relative to the start of the video.
        for para_instance in para['instances']:
            p = ParagraphSegment(start_time=hhmmss2seconds(para_instance['adjustedStart']),
                                                end_time=hhmmss2seconds(para_instance['adjustedEnd']),
                                                text=para['text'],
                                                language=para['language'],
                                                speaker=speakers.get(para['speakerId'], "Unknown Speaker"),
                                                confidence=para['confidence'])
            transcript.paragraphs.append(p)

    # It's really weird the way that AVI returns the paragraphs, so just
    # to make sure there's not some goofy "They said 'Thank you' 42 times
    # so we'll bundle it into a single entry", I'm going to sort the 
    # paragraphs by start time and that should do the trick.
    transcript.paragraphs = sorted(transcript.paragraphs, key=lambda x: x.start_time) 

    # Paragraphs -> text is pretty easy.
    transcript.text = " ".join([x.text for x in transcript.paragraphs])

    # Words is harder...because we have to respect the speaker. Also note
    # that we don't get word-level timestamps.  I'm going to synthesize them
    # by chopping them into evenly-spaced chunks.  It's obviously not going
    # to be right, but it's something that's close.
    for para in transcript.paragraphs:
        words = para.text.split()
        duration = para.duration() / len(words)
        offset = 0
        for word in words:
            transcript.words.append(WordSegment.from_str(word, 
                                                            start_time=offset, 
                                                            end_time=offset + duration,
                                                            speaker=para.speaker,
                                                            language=para.language,
                                                            confidence=para.confidence))
            
    tool_output.output.outputs['transcript'] = transcript
    # Audio effects
    # should be pretty straightforward
    ae_map = {'Silence': AudioEffectType.SILENCE,
              'Speech': AudioEffectType.SPEECH,
              'Music Playing': AudioEffectType.MUSIC}
    audio_effects = AudioEffects(media_duration=transcript.media_duration)    
    for audio_effect in video['insights']['audioEffects']:
        for ae_inst in audio_effect['instances']:
            audio_effects.effects.append(AudioEffectSegment(start_time=hhmmss2seconds(ae_inst['adjustedStart']),
                                                            end_time=hhmmss2seconds(ae_inst['adjustedEnd']),
                                                            confidence=ae_inst['confidence'],
                                                            effect=ae_map.get(audio_effect['type'], AudioEffectType.UNKNOWN),
                                                            name=audio_effect['type'].lower()))
    tool_output.output.outputs['audio_effects'] = audio_effects

    # TODO: brands?
    
    # Setting up the thumbnail cache here.  If done correctly then we should
    # have efficient image representation in both YAML and internally because
    # we'll only be generating a new image object for each thumbnail and reusing
    # it all over the place.  Sadly, this doesn't help us for JSON.
    class ThumbnailCache:
        def __init__(self, data_map: dict):
            self.data_map = data_map
            self.cache = dict()

        def get(self, thumbnail_id: str):
            if thumbnail_id not in self.cache:
                if thumbnail_id not in self.data_map:
                    self.cache[thumbnail_id] = None
                else:                    
                    pil_img = PIL.Image.open(io.BytesIO(self.data_map[thumbnail_id]))
                    img = Image()                    
                    img.set_image(pil_img, f"{thumbnail_id}.png", "png")
                    self.cache[thumbnail_id] = img
            return self.cache[thumbnail_id]

    thumbnail_cache = ThumbnailCache(native['thumbnails'])

    # detectedObjects
    detected = DetectedObjects(media_duration=hhmmss2seconds(video['insights']['duration']))
    for obj in video['insights']['detectedObjects']:
        for inst in obj['instances']:
            detobj = DetectedObject(start_time=hhmmss2seconds(inst['adjustedStart']),
                                    end_time=hhmmss2seconds(inst['adjustedEnd']),
                                    confidence=inst['confidence'],                                    
                                    image=thumbnail_cache.get(obj['thumbnailId']),
                                    name=obj['displayName'],                                    
                                    type=obj['type'],
                                    wikidata_id=obj['wikiDataId'])
            detected.objects.append(detobj)
    tool_output.output.outputs['detected_objects'] = detected

    # framePatterns
    videopats = VideoPatterns(media_duration=hhmmss2seconds(video['insights']['duration']))
    vpmap = {'Black': VideoPatternType.BLACK,
             'ColorBars': VideoPatternType.COLORBARS,
             }
    for pat in video['insights']['framePatterns']:
        for inst in pat['instances']:
            vpat = VideoPattern(start_time=hhmmss2seconds(inst['adjustedStart']),
                                end_time=hhmmss2seconds(inst['adjustedEnd']),
                                confidence=pat['confidence'],
                                name=pat['patternType'],
                                pattern=vpmap.get(pat['patternType'], VideoPatternType.OTHER))
            videopats.patterns.append(vpat)
    tool_output.output.outputs['video_patterns'] = videopats

    # keywords
    # labels
    # namedLocations
    # ocr
    # scenes
    # shots
    # topics



    return tool_output



        


def key_finder(data: Any, key: str) -> list:
    """Find the values for the given key no matter where
       it is in the data structure"""
    res = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key:
                res.append(v)
            else:
                if isinstance(v, dict):
                    res.extend(key_finder(v, key))
                elif isinstance(v, (list, set, tuple)):
                    for i in v:
                        res.extend(key_finder(i, key))
    elif isinstance(data, (set, list, tuple)):
        for i in data:
            res.extend(key_finder(i, key))

    return res


if __name__ == "__main__":
    import yaml
    import PIL.Image
    with open("../../test.yaml") as f:
        data = yaml.safe_load(f)
    
    t = parse_vi_data(data)
    print(t.model_dump_yaml())