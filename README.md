# Dalle WebIO

Web UI for Dalle 3



## Install

1. Clone this repository and change to its directory.
2. Install python >= 3.10.
3. Install the requirements: `pip install -r requirements`.
4. Run the script: `python dalle_webio.py`.



## Usage

0. Create an Azure OpenAI resource and deploy the `dall-e-3` model.

1. Enter the required information and submit. A client for calling the model will be created based on it.

   ![image-20240404135531797](https://raw.githubusercontent.com/nighty90/ImgRepository/main/img/image-20240404135531797.png)

2. Enter your prompt, set the parameters and then generate.

   ![image-20240404134707035](https://raw.githubusercontent.com/nighty90/ImgRepository/main/img/image-20240404134707035.png)



## Config

You can create a JSON file `settings.json` under the same directory as the script. It will overwrite the default values of the settings and parameters if the new values are valid. Supported properties are as follows.

+ `key`: Resource key. String.

+ `endpoint`: Resource endpoint. String.

+ `deployment`: Deployment name. String.

+ `rpm`: Allowed requests per minute. Integer. The script will send requests under this limit. 

+ `save_dir`: Save directory. String. When click the **save** button, the generated image will be saved to this directory. 

+ `as_is`: Whether to use the AS-IS prompt prefix to reduce the chance that the prompt is revised. Bool.

+ `num`: Number of images to be generated when click the **generate** button. Integer.

+ `api_version`: API version. Allowed values are "2024-02-01" and "2024-02-15-preview".

+ `style`: Style of the generated images. Allowed values are "natural" and "vivid" (for hyper-realistic / dramatic images).

+ `quality`: Quality of the generated images. Allowed values are "hd" and "standard".

+ `size`: Size of the generated images in the format of "<width>x<height>". Allowed values are "1792x1024", "1024x1024" and "1024x1792".

  