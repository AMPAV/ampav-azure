"""Render as HTML so I can see what I'm getting back"""
# In reality, we should consider putting renderers in all of the classes so it
# "just works" for the common case.  But for now, I'm going to deal with them
# here.  Also, hardcoding is dumb so don't do this for an official implementation

import logging
from typing import Any

from ampav.core.schema import *
from html import escape
from ampav.core.schema.basemodel import AmpAVBaseModel
from base64 import b64encode
from PIL.Image import Image as PILImage

from ampav.core.schema.image import serialize_pil_image

def render_data(result: Any, title: str) -> str:

    def dump_structure(data):
        match data:
            case AmpAVBaseModel():
                x = f"<h3>{data.__module__}</h3>"
                x += dump_structure({k: getattr(data, k) for k in data.__class__.model_fields.keys()})
                #x += dump_structure(data.model_dump())
                
                return x
            case PILImage():
                return f'<img src="{serialize_pil_image(data)}">'                
            case str():
                if data.startswith('data:image/png;base64,'):
                    return f'<img src="{data}">'
                else:
                    return escape(data)
            case list() | set():
                x = "<table><tr><th>Index</th><th>Value</th></tr>"
                for i, y in enumerate(data):
                    x += f"<tr><td>{i}</td><td>{dump_structure(y)}</td></tr>"
                x += "</table>"
                return x
            case dict():
                x = "<table><tr><th>Key</th><th>Value</th></tr>"
                for k, v in data.items():
                    x += f"<tr><td>{escape(k)}</td><td>{dump_structure(v)}</td></tr>"
                x += "</table>"
                return x
            case int():
                return data
            case float():
                return f"{data:0.2f}"
            case bool():
                return f"<b>{data}</b>"
            case bytes():
                return b64encode(data).decode()
            case None:
                return "<b>None</b>"
            case _:
                logging.warning(f"Unknown data type when rendering: {type(data)}: {data}")

    output = f"""<html>
  <head>
    <title>{title}</title>
    <style>
        table {{
            border-collapse: collapse;
            width: 100%;
        }}
        th {{
            text-align: left;
            vertical-align: top;
        }}
        td {{
            text-align: left;
            vertical-align: top;            
        }}
        tr {{
            width: 100%;
        }}
        table, th, td, tr {{
            border: 1px solid;
        }}

    </style>
  </head>
  <body>
    <h1>{title}</h1>
"""
    output += dump_structure(result)
    output += "  </body>\n</html>"
    return output



            

