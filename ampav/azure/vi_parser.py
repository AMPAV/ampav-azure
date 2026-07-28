#!/bin/env python3.12
import io
from typing import Any

import yaml
from ampav.core.schema.annotation import Annotation, AnnotationType, Annotations
from ampav.core.schema.audio import AudioEffect, AudioEffectType, AudioEffects, AudioEffect
from ampav.core.schema.named_entity import NamedEntities, NamedEntity, NamedEntityType
from ampav.core.schema.object import DetectedObject, DetectedObjects
from ampav.core.schema.sentiment import Sentiment, SentimentType, Sentiments
from ampav.core.schema.transcript import Transcript
from ampav.core.schema.video import KeyFrame, VideoOcr, VideoOcrResult, VideoPattern, VideoPatternType, VideoPatterns, VideoSegment, VideoSegmentType, VideoSegments
from ampav.core.utils import hhmmss2seconds, pt2seconds
from ampav.core.schema.compound import CompoundOutput
from ampav.core.schema.segments import ConfidenceSegment, ParagraphSegment, Segment, WordSegment
from ampav.core.schema.tool import ToolOutput
from ampav.core.schema.image import BoundingBox, Image
import PIL.Image
import logging
from collections import namedtuple


# Setting up the thumbnail cache here to make image instantiation faster.
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
                img = Image(filename=f"{thumbnail_id}.png", image=pil_img)
                self.cache[thumbnail_id] = img
        return self.cache[thumbnail_id]


MediaInfo = namedtuple('MediaInfo', ['duration', 'width', 'height'])
    
def parse_vi_data(native: dict):
    tool_output = ToolOutput(tool_name="Azure Video Indexer",
                                tool_version="1.0",
                                output=CompoundOutput())


    # if the data looks like it's a viraw that we're generating internally
    # we'll use that, otherwise we'll make the raw data (which probably came
    # directly from AVI) look like what we're generating.
    if native.get('format', None) != 'viraw' and 'data' not in native.keys():
        native = {'data': native,
                  'thumbnails': {},
                  'artifacts': {}}

    video = native['data']['videos'][0]
    insights = video['insights']    

    functab = {'audioEffects': ['audio_effects', do_audio_effects],
               'blocks': [None, None],
               'brands': ['named_entities', do_brands],
               'detectedObjects': ['detected_objects', do_detected_objects],
               'duration': [None, None],
               'emotions': ['annotations', do_emotions],
               'faces': [None, None],  # probably should do this some day
               'framePatterns': ['frame_patterns', do_frame_patterns],
               'keywords': ['annotations', do_annotations],
               'labels': ['annotations', do_labels],
               'language': [None, None],
               'languages': [None, None],
               'namedLocations': ['named_entities', do_named_locations],
               'namedPeople': ['named_entities', do_brands],
               'ocr': ['ocr', do_ocr],
               'ocrAnalyzedTokenCount': [None, None],
               'ocrMaxTokenCount': [None, None],
               'scenes': ['video_segments', do_video_segments],
               'sentiments': ['sentiments', do_sentiments],
               'shots': ['video_segments', do_video_segments],
               'sourceLanguage': [None, None],
               'sourceLanguages': [None, None],
               'speakers': [None, None], # this gets used by transcript, no need to handle
               'statistics': [None, None],
               'textualContentModeration': ['annotations', do_textual_content_moderation],
               'topics': ['annotations', do_annotations],
               'transcript': ['transcript', do_transcript],
               'version': [None, None],
               'visualContentModeration': ['annotations', do_visual_content_moderation]
               }

    # many things need the duration & frame size, so let's compute it here
    media_info = MediaInfo(hhmmss2seconds(insights['duration']),
                           video['width'], video['height'])


    # access the thumbnail cache
    thumbnail_cache = ThumbnailCache(native['thumbnails'])
    
    # go through the available insights and handle them.
    for k in insights:
        if k not in functab:
            # this is an insight we don't know how to convert, so warn about
            # it and ignore it.
            logging.warning(f"Unknown insight '{k}'.  Skipping.")
            logging.warning(yaml.safe_dump(insights[k]))
            continue
        dest, handler = functab[k]
        if dest is None:
            # we just ignore that one.
            continue

        count = handler(media_info, insights, k, tool_output.output.outputs, dest, thumbnail_cache,
                        native.get('artifacts', {}))
        if count == 0 or dest not in tool_output.output.outputs:
            logging.info(f"Expected {dest}, but it wasn't generated by {k}, {count} items generated")

    # fill in any last details.
    tool_output.parameters['filename'] = native['data'].get('name', None)
    tool_output.parameters['job_id'] = native['data'].get('id', None)

    return tool_output


def do_annotations(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    """Create annotations"""
    # Keywords and topics are very nearly the same, so we use the src_key to
    # handle the differences.
    differences = {'keywords': [AnnotationType.KEYWORD, 'text', None, []],
                   'topics': [AnnotationType.TOPIC, None, None,
                              ['iabName', 'iptcName', 'referenceId', 'referenceType', 'referenceUrl']],
                   #'emotions': [AnnotationType.EMOTION, 'type', []]  
    }
    annotations = []
    for item in insights[src_key]:
        a = Annotation(type=differences[src_key][0],
                       language=item.get('language', None))
        if differences[src_key][1]:
            a.text = item[differences[src_key][1]]
        if differences[src_key][2]:        
            a.label=item[differences[src_key][2]]
        for inst in item['instances']:
            a.instances.append(ConfidenceSegment(**instance2timeseg(inst),
                                                 confidence=item.get('confidence', None)))        
        if differences[src_key][3]:
            a.tool_private = {}
            for x in differences[src_key][3]:
                if x in item:
                    a.tool_private[x] = item[x]
        annotations.append(a)

    if annotations:
        if dest_key not in outputs:
            outputs[dest_key] = Annotations(media_duration=media_info.duration)
        outputs[dest_key].annotations.extend(annotations)
    return len(annotations)


def do_audio_effects(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    ae_map = {'Silence': AudioEffectType.SILENCE,
              'Speech': AudioEffectType.SPEECH,
              'Music Playing': AudioEffectType.MUSIC}
    res = []
    for item in insights[src_key]:
        a = AudioEffect(type=ae_map.get(item['type'], AudioEffectType.OTHER),
                                    label=item['type'])
        for inst in item['instances']:
            a.instances.append(ConfidenceSegment(**instance2timeseg(inst),
                                                 confidence=inst['confidence']))
        res.append(a)
    if res:
        if dest_key not in outputs:
            outputs[dest_key] = AudioEffects(media_duration=media_info.duration)
        outputs[dest_key].effects.extend(res)
    return len(res)


def do_brands(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res = []
    differences = {'brands': ('Brand', NamedEntityType.BRAND),
                   'namedPeople': ('namedPerson', NamedEntityType.PERSON)}
    for item in insights[src_key]:
        for inst in item['instances']:
            binst = NamedEntity(**instance2timeseg(inst),                                 
                                confidence=item['confidence'],
                                tool_private={'instanceSource': inst['instanceSource']},
                                text=item['name'],
                                entity_type=differences[src_key][0],
                                type=differences[src_key][1])
            for k in ('description', 'referenceId', 'referenceType', 'referenceUri'):
                if k in item:
                    binst.tool_private[k] = item[k]

            res.append(binst)

    if res:
        if dest_key not in outputs:
            outputs[dest_key] = NamedEntities()
        outputs[dest_key].spans.extend(res)
    return len(res)


def do_detected_objects(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res = []
    for item in insights[src_key]:
        detobj = DetectedObject(image=thumbnail_cache.get(item['thumbnailId']),
                                text=item['displayName'],
                                label=item['type'],
                                tool_private={'wikidata_id': item.get('wikiDataId', None)})
        for inst in item['instances']:
            detobj.instances.append(ConfidenceSegment(**instance2timeseg(inst),
                                                      confidence=inst['confidence']))            
        res.append(detobj)
    if res:
        if dest_key not in outputs:
            outputs[dest_key] = DetectedObjects(media_duration=media_info.duration)
        outputs[dest_key].objects.extend(res)
    return len(res)


def do_emotions(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res: list[Annotation] = []
    if 'emotions' in artifacts:
        # use the more detailed emotion information
        for emot in artifacts['emotions']:
            a = Annotation(type=AnnotationType.EMOTION,
                           label=emot['DominantEmotion']['Type'],
                           text=emot['Text'],
                           instances=[ConfidenceSegment(start_time=hhmmss2seconds(emot['TimeRange']['Start']),
                                                        end_time=hhmmss2seconds(emot['TimeRange']['End']),
                                                        confidence=emot['DominantEmotion']['Probability']/100)])
            res.append(a)
    else:
        # just use the insights for the video
        for item in insights[src_key]:
            a = Annotation(type=AnnotationType.EMOTION,
                           label=item['type'],
                           language=item.get('language', None))
            for inst in item['instances']:
                a.instances.append(ConfidenceSegment(**instance2timeseg(inst),
                                                    confidence=item.get('confidence', None)))
            res.append(a)

    if res:
        if dest_key not in outputs:
            outputs[dest_key] = Annotations(media_duration=media_info.duration)
        outputs[dest_key].annotations.extend(res)
        outputs[dest_key].merge_instances()


def do_frame_patterns(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res = []
    vpmap = {'Black': VideoPatternType.BLACK,
            'ColorBars': VideoPatternType.COLORBARS,
            'RollingCredits': VideoPatternType.CREDITS}
    for item in insights[src_key]:
        vpat = VideoPattern(type=vpmap.get(item['patternType'], VideoPatternType.OTHER),
                            label=item['patternType'])
        for inst in item['instances']:
            vpat.instances.append(ConfidenceSegment(**instance2timeseg(inst),
                                                   confidence=item['confidence']))
        res.append(vpat)
    if res:
        if dest_key not in outputs:
            outputs[dest_key] = VideoPatterns(media_duration=media_info.duration)
        outputs[dest_key].patterns.extend(res)
    return len(res)


def do_labels(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    """Create annotations for labels"""
    # This is very much like do_annotations but because the data is structured
    # differently it has to be unrolled in a different fashion.
    annotations = []    
    for item in insights[src_key]:
        a = Annotation(type=AnnotationType.LABEL,
                       text=item['name'],
                       language=item.get('language', None))
        for inst in item['instances']:
             a.instances.append(ConfidenceSegment(**instance2timeseg(inst), confidence=inst['confidence']))
        annotations.append(a)
    # we have to do a bit of a dance because annotations gets called more than once
    if annotations:
        if dest_key not in outputs:
            outputs[dest_key] = Annotations(media_duration=media_info.duration)
        outputs[dest_key].annotations.extend(annotations)        
    return len(annotations)


def do_named_locations(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res = []
    for item in insights[src_key]:
        for inst in item['instances']:
            linst = NamedEntity(**instance2timeseg(inst),
                                confidence=item['confidence'],
                                tool_private={'description': item['description'],
                                              'instanceSource': inst['instanceSource'],
                                              'referenceId': item['referenceId'],                                              
                                              'referenceUrl': item['referenceUrl']},
                                text=item['name'],
                                entity_type="namedLocation",
                                type=NamedEntityType.LOCATION)            
            res.append(linst)
    if res:
        if dest_key not in outputs:
            outputs[dest_key] = NamedEntities(media_duration=media_info.duration)
        outputs[dest_key].spans.extend(res)
    return len(res)


def do_ocr(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res = []
    for item in insights[src_key]:
        for inst in item['instances']:
            vocr = VideoOcrResult(bounding_box=BoundingBox(x=item['left'],
                                                           y=item['top'],
                                                           width=item['width'],
                                                           height=item['height']),
                                  angle=item['angle'],
                                  text=item['text'],
                                  language=item['language'],
                                  **instance2timeseg(inst),
                                  confidence=item['confidence'])
            res.append(vocr)
    if res:
        if dest_key not in outputs:
            outputs[dest_key] = VideoOcr(media_duration=media_info.duration)
        outputs[dest_key].ocr.extend(res)
    return len(res)


def do_sentiments(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res = []
    smap = {'Positive': SentimentType.POSITIVE,
            'Neutral': SentimentType.NEUTRAL,
            'Negative': SentimentType.NEGATIVE}
    for item in insights[src_key]:
        sent = Sentiment(type=smap.get(item['sentimentType'], SentimentType.UNKNOWN),
                         label=item['sentimentType'],
                         tool_private={'averageScore': item['averageScore']})
        for inst in item['instances']:
            sent.instances.append(Segment(**instance2timeseg(inst)))
        res.append(sent)    
    if res:
        if dest_key not in outputs:
            outputs[dest_key] = Sentiments(media_duration=media_info.duration)
        outputs[dest_key].sentiments.extend(res)
    return len(res)


def do_textual_content_moderation(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res = []
    if 'textualcontentmoderation' in artifacts:
        # use detailed information
        tcm = artifacts['textualcontentmoderation'].get('TextualContentModeration', [])
        for item in tcm:
            a = Annotation(type=AnnotationType.MATURE,
                           label="mature_language",
                           text=item['Word'])
            for inst in tcm['Instances']:
                a.instances.append(Segment(**instance2timeseg(inst),
                                           tool_private={'Type': inst['Type']}))
            res.append(a)
    else:
        # the summarized version doesn't give us anything, so we're going to
        # generate a singular instance that covers the entire video.
        res.append(Annotation(type=AnnotationType.MATURE,
                              label="mature_language",
                              text=f"The content contains {insights[src_key].get('bannedWordsCount', 0)} banned words",
                              instances=[ConfidenceSegment(start_time=0,
                                                           end_time=media_info.duration,
                                                           confidence=1)]))
    
    if res:
        if dest_key not in outputs:
            outputs[dest_key] = Annotations(media_duration=media_info.duration)
        outputs[dest_key].annotations.extend(res)      
        outputs[dest_key].merge_instances()  
    return len(res)



def do_transcript(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    if 'transcript' in artifacts:
        # Use the more detailed version
        transcript = Transcript(media_duration=media_info.duration)
        transcript.text = artifacts['transcript']['CombinedRecognizedPhrases'][0]['Display']
        language = artifacts['transcript']['Locale']
        transcript.languages = [language]
        for paragraph in artifacts['transcript']['RecognizedPhrases']:
            start = pt2seconds(paragraph['Offset'])
            end = start + pt2seconds(paragraph['Duration'])
            lang = paragraph.get('Locale', None)
            if lang is None:
                lang = language
            transcript.paragraphs.append(ParagraphSegment(start_time=start, 
                                                          end_time=end,
                                                          language=lang,
                                                          speaker=f"Speaker #{paragraph['Speaker']}",
                                                          text=paragraph['NBest'][0]['Display']))
            for word in paragraph['NBest'][0]['Words']:
                start = pt2seconds(word['Offset'])
                end = start + pt2seconds(word['Duration'])
                lang = transcript.paragraphs[-1].language
                transcript.words.append(WordSegment.from_str(word['Word'], 
                                                             start_time=start,
                                                             end_time=end,
                                                             language=lang,
                                                             confidence=word['Confidence']))
    else:
        # Use the summarized version
        logging.warning("Using summarized transcript:  word timing will not be accurate")
        # Load the speaker names.        
        speakers = {}
        if 'speakers' in insights:
            for speaker in insights['speakers']:
                speakers[speaker['id']] = speaker['name']

        # Let's get the transcript first.  AVI gives us the audio in paragraph
        # style chunks. We'll populate that and then create the rest of the
        # transcript types from that.        
        transcript = Transcript(media_duration=media_info.duration,
                                languages=insights['languages'])
                
        for item in insights[src_key]:
            # I don't know if AVI ever returns more than one instace per
            # paragraph, but just to be sure, I'm going to iterate.
            for inst in item['instances']:
                p = ParagraphSegment(**instance2timeseg(inst),
                                    text=item['text'],
                                    language=item['language'],
                                    speaker=speakers.get(item['speakerId'], "Unknown Speaker"))
                transcript.paragraphs.append(p)

                # the confidence (and timestamps) are at the paragraph level, so
                # that makes things harder.  Since we don't get word-level timestamps
                # I'm going to synthesize them by splitting them into even-sized
                # chunks within the paragraph range.
                pwords = item['text'].split()
                pduration = p.duration() / len(pwords)
                offset = p.start_time
                for word in pwords:
                    transcript.words.append(WordSegment.from_str(word, 
                                                                start_time=offset, 
                                                                end_time=offset + pduration,
                                                                speaker=p.speaker,
                                                                language=p.language,
                                                                confidence=item['confidence']))
                    offset += pduration
            # Paragraphs -> text is pretty easy.
            transcript.text = " ".join([x.text for x in transcript.paragraphs])

    outputs[dest_key] = transcript
    return 1    


def do_video_segments(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res = []
    # scenes and shots are structurally similar, with the biggest difference
    # being that shots have keyframes and scenes do not. 
    differences={'scenes': VideoSegmentType.SCENE,
                 'shots': VideoSegmentType.SHOT}
    for item in insights[src_key]:        
        keyframes = []
        for keyframe in item.get('keyFrames', []):
            for kinst in keyframe['instances']:
                keyframes.append(KeyFrame(time=hhmmss2seconds(kinst['adjustedStart']),
                                          frame=thumbnail_cache.get(kinst['thumbnailId'])))
        for inst in item['instances']:
            sinst = VideoSegment(**instance2timeseg(inst),
                                 type=differences[src_key],
                                 label=str(differences[src_key]).capitalize(),
                                 keyframes=keyframes)
            if 'tags' in item:
                sinst.tool_private = {'tags': item['tags']}
            res.append(sinst)

    if res:
        if dest_key not in outputs:
            outputs[dest_key] = VideoSegments(media_duration=media_info.duration)
        outputs[dest_key].segments.extend(res)
    return len(res)


def do_visual_content_moderation(media_info: MediaInfo, insights: dict, src_key: str, outputs: dict, dest_key: str, thumbnail_cache: ThumbnailCache, artifacts: dict):
    res = []
    for item in insights[src_key]:
        a = Annotation(type=AnnotationType.MATURE,
                       label="mature_images")
        for inst in item['instances']:
             a.instances.append(ConfidenceSegment(**instance2timeseg(inst), confidence=item['adultScore']))
        res.append(a)    
    if res:
        if dest_key not in outputs:
            outputs[dest_key] = Annotations(media_duration=media_info.duration)
        outputs[dest_key].annotations.extend(res)        
        outputs[dest_key].merge_instances()  
    return len(res)


def instance2timeseg(inst: dict) -> dict:
    """Convert an instance timestamp into a dict"""
    # I'm using adjusted{Start,End} because that's guaranteed to be
    # relative to the start of the video.

    return {'start_time': hhmmss2seconds(inst['adjustedStart']),
            'end_time': hhmmss2seconds(inst['adjustedEnd'])}

