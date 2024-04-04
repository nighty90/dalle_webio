import asyncio
import re
import sys
import logging
import json
import time
from io import BytesIO
from pathlib import Path
from collections import deque

from aiohttp import ClientSession, ClientConnectionError
from pywebio.input import input as input_text, input_group
from pywebio.output import put_row, put_column, put_scope, clear, remove, toast, popup
from pywebio.output import put_buttons, put_text, put_image, put_html, put_loading
from pywebio.pin import pin, put_input, put_textarea, put_checkbox, put_select
from pywebio.session import run_async, defer_call
from pywebio import start_server, config
from PIL import Image
from PIL.PngImagePlugin import PngInfo


AS_IS_PREFIX = "I NEED to test how the tool works with extremely simple prompts. DO NOT add any detail, just use it AS-IS:"
INVALID_CHARS_REGEX = r"[\\/*?\"<>|]"
MAX_PATH_LEN = 200
LEAST_NAME_LEN = 10

INPUT_BUTTON_GROUP_CSS = "display: flex; justify-content: center; margin-top: 8px; gap: 8px"
CELL_CSS = "min-height: 265px"
IMG_CARD_PROMPT_CSS = """
    display: -webkit-box;  
    -webkit-box-orient: vertical;  
    -webkit-line-clamp: 3;
    overflow: hidden; 
"""
IMG_CARD_CSS = "border: 1px solid white; border-radius: 0.25rem; padding: 10px"
ZOOM_CARD_PROMPT_CSS = "overflow: auto; max-height: 100px"
ZOOM_CARD_CSS = "padding: 10px"
CARD_BUTTON_GROUP_CSS = "display: flex; justify-content: start; gap: 8px"
RESULT_CSS = """
    display: grid; 
    grid-template-columns: repeat(auto-fill, 265px); 
    grid-gap: 15px; 
    overflow: auto;
    sliderbar-gutter: stable;
    min-height: 300px;
    max-height: 800px;
"""


class DalleImage:
    def __init__(self, prompt, revised_prompt, img: Image):
        self.prompt = prompt
        self.revised_prompt = revised_prompt
        self.img = img

    def save(self, save_path: Path):
        info = PngInfo()
        info.add_text("prompt", self.prompt)
        info.add_text("revised_prompt", self.revised_prompt)
        self.img.save(save_path, pnginfo=info)


class RateLimiter:  
    def __init__(self, allowance: int, period: float):  
        self.stamps = deque(maxlen=allowance)
        self.period = period  


    def allow(self):  
        now = time.time()
        if len(self.stamps) < self.stamps.maxlen or now - self.stamps[0] > self.period:
            self.stamps.append(now)
            return True
        return False

  
    async def wait(self, extra_wait=0):  
        while not self.allow(): 
            sleep_sec = self.stamps[0] + self.period - time.time() + extra_wait
            await asyncio.sleep(sleep_sec)  


class DalleClient:
    def __init__(self, init_inputs):
        self.deployment = init_inputs["deployment"]
        self.dalle_session = ClientSession(
            base_url=init_inputs["endpoint"], 
            headers={"api-key": init_inputs["key"]}
        )
        self.img_session = ClientSession()
        self.limiter = RateLimiter(allowance=init_inputs["rpm"], period=60)


    async def close_client(self):
        await asyncio.create_task(self.dalle_session.close())
        await asyncio.create_task(self.img_session.close())
        logging.info("Client closed")


    async def _call_dalle(self, prompt, **kwargs):
        api_version = kwargs.get("api_version", "2024-02-01")
        size = kwargs.get("size", "1024x1024")
        quality = kwargs.get("quality", "standard")
        dalle_style = kwargs.get("style", "vivid")
        try:
            logging.info("Sending dalle request")
            async with self.dalle_session.post(
                url=f"/openai/deployments/{self.deployment}/images/generations",
                params={"api-version": api_version},
                json={
                    "prompt": prompt,
                    "size": size,
                    "quality": quality,
                    "style": dalle_style
                }
            ) as resp:
                result = await resp.json()
                result["status"] = resp.status
                result["reason"] = resp.reason
        except ClientConnectionError as e:
            logging.error("Connection Error when calling Dalle: %s", e)
            return None
        return result
    

    def _process_dalle_response(self, result):
        if result is None:
            toast("An error occurred during calling Dalle", color="error", duration=5)
            return None
        if result["status"] == 429:
            msg = result["error"]["message"]
            wait_sec_match = re.search(r"retry after (\d+) second", msg)
            if not wait_sec_match:
                toast(f"Failed request: {result['status']} {result['reason']}: {msg}", color="warn", duration=5)
                logging.warning("Failed request: %d %s: %s", result["status"], result["reason"], msg)
            else:
                wait_sec = int(wait_sec_match.group(1))
                toast(f"429 error, please retry after {wait_sec} seconds", color="warn", duration=5)
                logging.warning("429 error, please retry after %d seconds", wait_sec)
            return None
        if result["status"] != 200:
            msg = result["error"]["message"]
            toast(f"Failed request: {result['status']} {result['reason']}: {msg}", color="warn", duration=5)
            logging.warning("Failed request: %d %s: %s", result["status"], result["reason"], msg)
            return None
        
        revised_prompt = result["data"][0]["revised_prompt"]
        img_url = result["data"][0]["url"]
        return revised_prompt, img_url
            

    async def _get_img(self, img_url):
        try:
            logging.info("Getting generated image")
            async with self.img_session.get(img_url) as response:
                img = Image.open(BytesIO(await response.read()))
        except ClientConnectionError as e:
            logging.error("Connection Error when getting image: %s", e)
            return None
        return img
    

    def _prepare_img_path(self, save_dir: Path, prompt: str, suffix: str):
        sanitized_prompt = re.sub(INVALID_CHARS_REGEX, "_", prompt)
        save_dir_path_len = len(str(save_dir.resolve()))
        allowed_name_len = MAX_PATH_LEN - save_dir_path_len - len(suffix) - LEAST_NAME_LEN
        if allowed_name_len <= 0:
            toast("The file path is too long, please choose another save directory", color="error", duration=5)
            logging.error("The file path is too long", color="error", duration=5)
            return None
        if not save_dir.exists():
            save_dir.mkdir()
        if len(sanitized_prompt) + len(suffix) > allowed_name_len:
            get_length = allowed_name_len - len(suffix) - 3
            sanitized_prompt = sanitized_prompt[:get_length] + "..."
        return save_dir / (sanitized_prompt + suffix)
    

    async def generate_one_image(self, save_dir: Path, stamp: str, as_is: bool, prompt: str, **kwargs):
        img_path = self._prepare_img_path(save_dir, prompt, f"-{stamp}.png")
        if not img_path:
            return
        full_prompt = f"{AS_IS_PREFIX} {prompt}" if as_is else prompt

        put_scope(stamp, scope="result", position=0).style(CELL_CSS)
        put_loading(scope=stamp)
        put_text("Waiting...", scope=stamp)
        await self.limiter.wait(extra_wait=1)

        put_text("Generating...", scope=stamp)
        result = await asyncio.create_task(self._call_dalle(full_prompt, **kwargs))
        result = self._process_dalle_response(result)
        if not result:
            remove(stamp)
            return

        put_text("Getting Image...", scope=stamp)
        revised_prompt, img_url = result
        img = await asyncio.create_task(self._get_img(img_url))
        if not img:
            toast("Fail to get image", color="error")
            remove(stamp)
            return
            
        clear(stamp)
        dalle_img = DalleImage(prompt, revised_prompt, img)
        self.put_img_card(dalle_img, img_path, stamp)
    

    def ui(self, settings: dict):
        api_version_opts = ["2024-02-01", "2024-02-15-preview"]
        style_opts = ["natural", "vivid"]
        quality_opts = ["standard", "hd"]
        size_opts = ["1024x1024", "1792x1024", "1024x1792"]

        save_dir = settings.get("save_dir", r".\saved_images")
        as_is = ["true"] if settings.get("as_is") in ["true", True] else []
        try:
            num = int(float(settings.get("num", 1)))
        except ValueError:
            num = 1
        api_version = settings["api_version"] if settings.get("api_version") in api_version_opts else "2024-02-01"
        style = settings["style"] if settings.get("style") in style_opts else "vivid"
        quality = settings["quality"] if settings.get("quality") in quality_opts else "standard"
        size = settings["size"] if settings.get("size") in size_opts else "1024x1024"

        put_html("<style>.modal-dialog {max-width: 750px}</style>")
        put_input("save_dir", label="save directory", value=save_dir, type="text")
        put_checkbox("as_is", label="", options=[("Use the AS-IS prompt prefix:", "true")], value=as_is)
        put_input("show_as_is", label="", value=AS_IS_PREFIX, readonly=True)
        put_textarea("prompt", label="prompt", rows=3)
        put_row([
            put_input("num", label="num", value=num, type="number"), None,
            put_select("api_version", label="api version", options=api_version_opts, value=api_version), None,
            put_select("style", label="style", options=style_opts, value=style), None, 
            put_select("quality", label="quality", options=quality_opts, value=quality), None, 
            put_select("size", label="size", options=size_opts, value=size)
        ])
        put_buttons(
            buttons=[
                {"label": "generate", "value": "generate", "color": "primary"}, 
                {"label": "exit", "value": "exit", "color": "danger"}
            ], 
            onclick=[self.generate, self.exit_session]
        ).style(INPUT_BUTTON_GROUP_CSS)
        put_html("<hr></hr>")
        put_scope("result").style(RESULT_CSS)


    def zoom_card(self, dalle_img: DalleImage):
        popup(title="Image", content=[
            put_column(
                content=[
                    put_image(dalle_img.img), None,
                    put_html(f"<p><b>Prompt: </b>{dalle_img.prompt}</p>").style(ZOOM_CARD_PROMPT_CSS),
                    put_html(f"<p><b>Revised: </b>{dalle_img.revised_prompt}</p>").style(ZOOM_CARD_PROMPT_CSS),
                ],
                size="auto 10px auto auto 10px 50px"
            ).style(ZOOM_CARD_CSS)
        ])

    
    def save_img(self, dalle_img: DalleImage, img_path: Path):
        dalle_img.save(img_path)
        info_str = f"Saved to {img_path.name}"
        toast(info_str)
        logging.info(info_str)


    def set_prompt(self, prompt):
        pin.prompt = prompt


    def put_img_card(self, dalle_img: DalleImage, img_path: Path, scope: str):
        put_column(
            content=[
                put_image(dalle_img.img), None,
                put_html(f"<p><b>Prompt: </b>{dalle_img.prompt}</p>").style(IMG_CARD_PROMPT_CSS),
                put_html(f"<p><b>Revised: </b>{dalle_img.revised_prompt}</p>").style(IMG_CARD_PROMPT_CSS), None,
                put_buttons(
                    buttons=["zoom", "save", "delete"], 
                    onclick=[
                        lambda: self.zoom_card(dalle_img),
                        lambda: self.save_img(dalle_img, img_path),
                        lambda: remove(scope)
                    ]
                ).style(CARD_BUTTON_GROUP_CSS),
                put_buttons(
                    buttons=["use prompt", "use revised"], 
                    onclick=[
                        lambda: self.set_prompt(dalle_img.prompt),
                        lambda: self.set_prompt(dalle_img.revised_prompt)
                    ]
                ).style(CARD_BUTTON_GROUP_CSS)
            ], 
            size="auto 10px auto auto 10px 50px",
            scope=scope
        ).style(IMG_CARD_CSS)


    async def generate(self):
        save_dir = await pin.save_dir
        prompt = await pin.prompt
        num = await pin.num
        api_version = await pin.api_version
        as_is = await pin.as_is
        style = await pin.style
        quality = await pin.quality
        size = await pin.size

        if not prompt:
            toast("Prompt is empty", color="error", duration=3)
            return
        
        if not num:
            toast("Num must be an positive integer", color="error", duration=3)
            return

        save_dir = Path(save_dir)
        as_is = bool(as_is[0]) if as_is else False

        timestamp = int(time.time() * 1_000_000)
        for i in range(num):
            stamp = str(timestamp + i)
            run_async(self.generate_one_image(
                save_dir=save_dir, stamp=stamp, as_is=as_is,
                prompt=prompt, api_version=api_version, style=style, quality=quality, size=size,
            ))


    async def exit_session(self):
        await asyncio.create_task(self.close_client())
        toast("Exited", color="error")
        sys.exit()


def read_settings(setting_path: Path):
    if not setting_path.exists():
        return {}

    found_str = "Found setting file"
    toast(found_str)
    logging.info(found_str)
    try:
        with setting_path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except json.decoder.JSONDecodeError:
        fail_str = "Failed to parse setting file"
        toast(fail_str, color="error", duration=5)
        logging.error(fail_str)
        return {}


async def main():
    setting_path = Path("./settings.json")
    settings = read_settings(setting_path)
    key = settings.get("key", "")
    endpoint = settings.get("endpoint", "")
    deployment = settings.get("deployment", "Dalle3")
    rpm = settings.get("rpm", 3)
    init_inputs = await input_group(label="Settings", inputs=[
        input_text(label="AOAI Key: ", value=key, type="text", name="key", required=True),
        input_text(label="AOAI Endpoint: ", value=endpoint, type="url", name="endpoint", required=True),
        input_text(label="Dalle3 Deployment: ", value=deployment, type="text", name="deployment", required=True),
        input_text(label="RPM (Requests Per Minute): ", value=rpm, type="number", name="rpm", required=True),
    ])
    client = DalleClient(init_inputs)
    logging.info("Client created")
    defer_call(lambda: asyncio.create_task(client.close_client()))
    client.ui(settings)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
    config(theme="dark")
    start_server(main, host="127.0.0.1", auto_open_webbrowser=True)
    