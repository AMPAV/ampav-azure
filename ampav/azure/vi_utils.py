#!/bin/env python3.12
import io
from typing import Any
from ampav.core.schema.audio import AudioEffectType, AudioEffects, AudioEffectSegment
from ampav.core.schema.named_entity import NamedEntities, NamedEntity, NamedEntityType
from ampav.core.schema.object import DetectedObject, DetectedObjects
from ampav.core.schema.transcript import Transcript
from ampav.core.schema.video import KeyFrame, VideoOcr, VideoOcrResult, VideoPattern, VideoPatternType, VideoPatterns, VideoSegment, VideoSegmentType, VideoSegments
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
        native = {'data': native,
                  'thumbnails': {}}

    # Load the speaker names.    
    video = native['data']['videos'][0]
    insights = video['insights']
    speakers = {}
    for speaker in insights['speakers']:
        speakers[speaker['id']] = speaker['name']
    
    # Let's get the transcript first.  AVI gives us the audio in paragraph
    # style chunks. We'll populate that and then create the rest of the
    # transcript types from that.        
    transcript = Transcript(media_duration=hhmmss2seconds(insights['duration']),
                            languages=insights['languages'])
            
    for para in insights['transcript']:
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
    for audio_effect in insights['audioEffects']:
        for ae_inst in audio_effect['instances']:
            audio_effects.effects.append(AudioEffectSegment(start_time=hhmmss2seconds(ae_inst['adjustedStart']),
                                                            end_time=hhmmss2seconds(ae_inst['adjustedEnd']),
                                                            confidence=ae_inst['confidence'],
                                                            type=ae_map.get(audio_effect['type'], AudioEffectType.UNKNOWN),
                                                            label=audio_effect['type'].lower()))
    tool_output.output.outputs['audio_effects'] = audio_effects

    # brands
    named_entities = NamedEntities()
    for brand in insights['brands']:
        for inst in brand['instances']:
            binst = NamedEntity(start_time=hhmmss2seconds(inst['adjustedStart']),
                                end_time=hhmmss2seconds(inst['adjustedEnd']),
                                confidence=brand['confidence'],
                                tool_private={'description': brand['description'],
                                              'instanceSource': inst['instanceSource'],
                                              'referenceId': brand['referenceId'],
                                              'referenceType': brand['referenceType'],
                                              'referenceUrl': brand['referenceUrl']},
                                text=brand['name'],
                                entity_type="Brand",
                                type=NamedEntityType.BRAND)
            named_entities.spans.append(binst)

    tool_output.output.outputs['named_entities'] = named_entities

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
    detected = DetectedObjects(media_duration=hhmmss2seconds(insights['duration']))
    for obj in insights['detectedObjects']:
        for inst in obj['instances']:
            detobj = DetectedObject(start_time=hhmmss2seconds(inst['adjustedStart']),
                                    end_time=hhmmss2seconds(inst['adjustedEnd']),
                                    confidence=inst['confidence'],                                    
                                    image=thumbnail_cache.get(obj['thumbnailId']),
                                    text=obj['displayName'],                                    
                                    label=obj['type'],
                                    tool_private={'wikidata_id': obj['wikiDataId']})
                                    
            detected.objects.append(detobj)
    tool_output.output.outputs['detected_objects'] = detected

    # framePatterns
    videopats = VideoPatterns(media_duration=hhmmss2seconds(insights['duration']))
    vpmap = {'Black': VideoPatternType.BLACK,
             'ColorBars': VideoPatternType.COLORBARS,
             }
    for pat in insights['framePatterns']:
        for inst in pat['instances']:
            vpat = VideoPattern(start_time=hhmmss2seconds(inst['adjustedStart']),
                                end_time=hhmmss2seconds(inst['adjustedEnd']),
                                confidence=pat['confidence'],
                                label=pat['patternType'],
                                type=vpmap.get(pat['patternType'], VideoPatternType.OTHER))
            videopats.patterns.append(vpat)
    tool_output.output.outputs['video_patterns'] = videopats

    # keywords
    # labels

    # namedLocations
    for loc in insights['namedLocations']:
        for inst in loc['instances']:
            linst = NamedEntity(start_time=hhmmss2seconds(inst['adjustedStart']),
                                end_time=hhmmss2seconds(inst['adjustedEnd']),
                                confidence=loc['confidence'],
                                tool_private={'description': loc['description'],
                                              'instanceSource': inst['instanceSource'],
                                              'referenceId': loc['referenceId'],                                              
                                              'referenceUrl': loc['referenceUrl']},
                                text=loc['name'],
                                entity_type="Location",
                                type=NamedEntityType.LOCATION)
            named_entities.spans.append(linst)

    # ocr
    videoocr = VideoOcr(media_duration=hhmmss2seconds(insights['duration']))
    for ocr in insights['ocr']:
        for inst in ocr['instances']:
            vocr = VideoOcrResult(x1=ocr['left'],
                                  y1=ocr['top'],
                                  x2=ocr['left'] + ocr['width'],
                                  y2=ocr['top'] - ocr['height'],
                                  angle=ocr['angle'],
                                  text=ocr['text'],
                                  language=ocr['language'],
                                  start_time=hhmmss2seconds(inst['adjustedStart']),
                                  end_time=hhmmss2seconds(inst['adjustedEnd']),
                                  confidence=ocr['confidence'])
            videoocr.ocr.append(vocr)
    tool_output.output.outputs['video_ocr'] = videoocr

    # scenes & shots
    vsegs = VideoSegments(media_duration=hhmmss2seconds(insights['duration']))
    for skey, stype, slabel in (('scenes', VideoSegmentType.SCENE, 'Scene'),
                               ('shots', VideoSegmentType.SHOT, 'Shot')):
        for sthing in insights[skey]:
            keyframes = []
            for keyframe in sthing.get('keyFrames', []):
                for kinst in keyframe['instances']:
                    keyframes.append(KeyFrame(time=hhmmss2seconds(kinst['adjustedStart']),
                                              frame=thumbnail_cache.get(kinst['thumbnailId'])))
            for inst in sthing['instances']:
                sinst = VideoSegment(start_time=hhmmss2seconds(inst['adjustedStart']),
                                     end_time=hhmmss2seconds(inst['adjustedEnd']),
                                     type=stype,
                                     label=slabel,
                                     keyframes=keyframes)
                vsegs.segments.append(sinst)
    tool_output.output.outputs['video_segments'] = vsegs

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