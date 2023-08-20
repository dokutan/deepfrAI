# deepfrAI

An ESRGAN model to deep fry images.

## How to use this?

Download the model from the releases page, [chaiNNer](https://github.com/chaiNNer-org/chaiNNer) is the easiest way to use the trained model. Other tools that work with ESRGAN models should be compatible as well.

## Used ressources

- Dataset https://www.kaggle.com/datasets/sayangoswami/reddit-memes-dataset
- traiNNer: https://github.com/victorca25/traiNNer
- Training guide: https://rentry.org/How2ESRGAN

## Training
### Preparing the dataset
1. Download and extract the dataset
2. Deep fry the data set ``python deepfry.py``, edit deepfry.py if necessary
3. Manually move the images into the correct directories under ``./dataset``
    - Most original images to ``./dataset/train/lr``
    - The corresponding deep fried images to ``./dataset/train/hr``
    - Some original images to ``./dataset/val/lr``
    - The corresponding deep fried images to ``./dataset/val/hr``

### Training
```
cd traiNNer/codes
python3 train.py -opt train_sr.yml
```

I trained the model for 40000 iterations.
