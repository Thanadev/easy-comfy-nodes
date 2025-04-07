import requests
import base64
import io
import numpy as np
from PIL import Image, ImageOps
import torch
import boto3
import rembg
import comfy
import pillow_avif


class HttpPostNode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"url": ("STRING", {"default": ""}), "body": ("DICT",)}}
    RETURN_TYPES = ("INT", )
    RETURN_NAMES=("status_code",)
    FUNCTION = "execute"
    CATEGORY = "HTTP"
    OUTPUT_NODE=True

    def execute(self, url, body):
        response = requests.post(url, json=body)
        print(response, response.status_code, response.text)
        return (response.status_code,)

class EmptyDictNode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {}}
    RETURN_TYPES = ("DICT", )
    RETURN_NAMES=("dict",)
    FUNCTION = "execute"
    CATEGORY = "DICT"

    def execute(self):
        return ({},)

class AssocStrNode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"dict": ("DICT",), "key": ("STRING", {"default": ""}), "value": ("STRING", {"default": ""})}}
    RETURN_TYPES = ("DICT", )
    RETURN_NAMES=("dict",)
    FUNCTION = "execute"
    CATEGORY = "DICT"

    def execute(self, dict, key, value):
        return ({**dict, key: value},)

class AssocDictNode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"dict": ("DICT",), "key": ("STRING", {"default": ""}), "value": ("DICT", {"default": {}})}}
    RETURN_TYPES = ("DICT", )
    RETURN_NAMES=("dict",)
    FUNCTION = "execute"
    CATEGORY = "DICT"

    def execute(self, dict, key, value):
        return ({**dict, key: value},)

class AssocImgNode:
    @classmethod
    def INPUT_TYPES(s):
        return {
                   "required": {
                       "dict": ("DICT",),
                       "key": ("STRING", {"default": ""}),
                       "value": ("IMAGE", {"default": ""}),
                   },
                   "optional": {
                       "format": ("STRING", {"default": "webp"}),
                       "quality": ("INT", {"default": 92})
                   }
               }
    RETURN_TYPES = ("DICT", )
    RETURN_NAMES=("dict",)
    FUNCTION = "execute"
    CATEGORY = "DICT"

    def execute(self, dict, key, value, format="webp", quality=92):
        image = Image.fromarray(np.clip(255. * value[0].cpu().numpy(), 0, 255).astype(np.uint8))
        buffered = io.BytesIO()
        image.save(buffered, format=format, quality=quality)
        img_bytestr =  base64.b64encode(buffered.getvalue())
        return ({**dict, key: (bytes(f'data:image/{format};base64,', encoding='utf-8') + img_bytestr).decode() },)

def loadImageFromUrl(url):
    # Lifted mostly from https://github.com/sipherxyz/comfyui-art-venture/blob/main/modules/nodes.py#L43
    if url.startswith("data:image/"):
        i = Image.open(io.BytesIO(base64.b64decode(url.split(",")[1])))
    elif url.startswith("s3://"):
        s3 = boto3.client('s3')
        bucket, key = url.split("s3://")[1].split("/", 1)
        obj = s3.get_object(Bucket=bucket, Key=key)
        i = Image.open(io.BytesIO(obj['Body'].read()))
    else:
        response = requests.get(url, timeout=5)
        if response.status_code != 200:
            raise Exception(response.text)

        i = Image.open(io.BytesIO(response.content))

    i = ImageOps.exif_transpose(i)

    if i.mode != "RGBA":
        i = i.convert("RGBA")

    # recreate image to fix weird RGB image
    alpha = i.split()[-1]
    image = Image.new("RGB", i.size, (0, 0, 0))
    image.paste(i, mask=alpha)

    image = np.array(image).astype(np.float32) / 255.0
    image = torch.from_numpy(image)[None,]
    if "A" in i.getbands():
        mask = np.array(i.getchannel("A")).astype(np.float32) / 255.0
        mask = 1.0 - torch.from_numpy(mask)
    else:
        mask = torch.zeros((64, 64), dtype=torch.float32, device="cpu")

    return (image, mask)

class LoadImageFromUrlNode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"url": ("STRING", {"default": ""})}}
    RETURN_TYPES = ("IMAGE", "MASK")
    RETURN_NAMES=("image", "mask")
    FUNCTION = "execute"
    CATEGORY = "HTTP"

    def execute(self, url):
        return {"result": loadImageFromUrl(url)}

class LoadImagesFromUrlsNode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"urls": ("STRING", {"default": "", "multiline": True, "dynamicPrompts": False})}}
    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES=("images",)
    FUNCTION = "execute"
    CATEGORY = "HTTP"

    def execute(self, urls):
        print(urls.split("\n"))
        images = [loadImageFromUrl(u)[0] for u in urls.split("\n")]
        firstImage = images[0]
        restImages = images[1:]
        if len(restImages) == 0:
            return (firstImage,)
        else:
            image1 = firstImage
            for image2 in restImages:
                if image1.shape[1:] != image2.shape[1:]:
                    image2 = comfy.utils.common_upscale(image2.movedim(-1, 1), image1.shape[2], image1.shape[1], "bilinear", "center").movedim(1, -1)
                image1 = torch.cat((image1, image2), dim=0)
            return (image1,)

class S3Upload:
    """
    Uploads first file from VHS_FILENAMES from ComfyUI-VideoHelperSuite to S3.

    See also: https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite
    """

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "filenames": ("VHS_FILENAMES",),
                "s3_bucket": ("STRING", {"default": ""}),
                "s3_object_name": ("STRING", {"default": "default/result.webp"}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("s3_url",)
    OUTPUT_NODE = True
    CATEGORY = "Video"
    FUNCTION = "execute"

    def execute(
        self,
        filenames=(),
        s3_bucket="",
        s3_object_name="",
    ):
        s3 = boto3.resource('s3')
        s3.Bucket(s3_bucket).upload_file(filenames[1][1], s3_object_name)
        s3url = f's3://{s3_bucket}/{s3_object_name}'
        print(f'Uploading file to {s3url}')
        return (s3url,)


NODE_CLASS_MAPPINGS = {
    "EZHttpPostNode": HttpPostNode,
    "EZEmptyDictNode": EmptyDictNode,
    "EZAssocStrNode": AssocStrNode,
    "EZAssocDictNode": AssocDictNode,
    "EZAssocImgNode": AssocImgNode,
    "EZLoadImgFromUrlNode": LoadImageFromUrlNode,
    "EZLoadImgBatchFromUrlsNode": LoadImagesFromUrlsNode,
    "EZS3Uploader": S3Upload,
    "EZRemoveImgBackground": RemoveImageBackground
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "EZHttpPostNode": "HTTP POST",
    "EZEmptyDictNode": "Empty Dict",
    "EZAssocStrNode": "Assoc Str",
    "EZAssocDictNode": "Assoc Dict",
    "EZAssocImgNode": "Assoc Img",
    "EZLoadImgFromUrlNode": "Load Img From URL (EZ)",
    "EZLoadImgBatchFromUrlsNode": "Load Img Batch From URLs (EZ)",
    "EZS3Uploader": "S3 Upload (EZ)",
    "EZRemoveImgBackground": "Remove Img Background (EZ)"
}
