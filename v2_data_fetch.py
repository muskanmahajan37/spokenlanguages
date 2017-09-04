from __future__ import print_function
import torchaudio
import torch.utils.data as data
import os
import errno
import random
import shutil
import json
import math
from itertools import accumulate

import requests
import bs4
import chardet

# heavily inspired by:
# https://github.com/patyork/python-voxforge-download/blob/master/python-voxforge-download.ipynb


class VOXFORGE(data.Dataset):
    """`Voxforge <http://voxforge.org>`_ Dataset.

    Args:
        TODO: update documentation
        basedir (string): Root directory of dataset.
        randomize (bool, optional): Gets a random selection of files for download.
        dev_mode(bool, optional): if true, clean up is not performed on downloaded
            files.  Useful to keep raw audio and transcriptions.
    """

    URLS = {
        "de": 'http://www.repository.voxforge1.org/downloads/de/Trunk/Audio/Main/16kHz_16bit/',
        "en": 'http://www.repository.voxforge1.org/downloads/SpeechCorpus/Trunk/Audio/Main/16kHz_16bit/',
        "fr": 'http://www.repository.voxforge1.org/downloads/fr/Trunk/Audio/Main/16kHz_16bit/',
        "sp": 'http://www.repository.voxforge1.org/downloads/es/Trunk/Audio/Main/16kHz_16bit/',
        "it": 'http://www.repository.voxforge1.org/downloads/it/Trunk/Audio/Main/16kHz_16bit/',
    }

    LABELS = dict([[x, i] for i, x in enumerate(sorted(list(URLS.keys())))])

    SPLITS = ["train", "valid", "test"]

    # set random seed
    random.seed(12345)

    def __init__(self, basedir, transform=None, target_transform=None,
                 langs=["de", "en"], ratios=[0.7, 0.1, 0.2], split="train",
                 label_type="lang", download=False, num_zips=10, randomize=False,
                 dev_mode=False):
        _make_dir_iff(basedir)
        self.basedir = basedir
        self.rand = randomize
        self.dev_mode = dev_mode
        self.num_zips = num_zips
        self.langs = langs
        self.split = split
        self.transform = transform
        self.target_transform = target_transform
        self.allow_anon = False
        self.processeddir = os.path.join(basedir, "processed")
        _make_dir_iff(self.processeddir)
        _make_dir_iff(os.path.join(self.processeddir, "audio"))
        _make_dir_iff(os.path.join(self.processeddir, "prompts"))

        if download:
            self.batch_download(num_zips, self.rand, self.allow_anon)
            self.extract_all()

        audiodir = os.path.join(self.processeddir, "audio")

        audiomanifest = [os.path.join(audiodir, fp) for fp in os.listdir(audiodir)]

        if label_type == "lang":
            audiolabels = [os.path.basename(l).split("__", 1)[0] for l in audiomanifest]
        else:
            with open(os.path.join(self.processeddir, "prompts", "prompts.json"), "r") as json_f:
                prompts = json.load(json_f)
            audiolabels = []
            for l in audiomanifest:
                l = os.path.basename(l)
                lang, spkr, fp_a = l.split("__")
                fp_a = fp_a.rsplit(".", 1)[0]
                audiolabels.append(prompts[lang][spkr][fp_a])

        num_files = len(audiomanifest)
        split_pts = list(accumulate([math.floor(num_files * r) for r in ratios]))
        split_pts = [range(0, split_pts[0]),
                     range(split_pts[0], split_pts[1]),
                     range(split_pts[1], num_files)]
        self.splits = {spn: spi for spn, spi in zip(self.SPLITS, split_pts)}
        self.data, self.labels = audiomanifest, audiolabels

    def __getitem__(self, index):
        """
        Args:
            index (int): Index

        Returns:
            tuple: (audio, label) where target is index of the target class.
        """
        audio, sr = torchaudio.load(self.data[index], normalization=True)
        target = self.labels[index]
        assert sr == 16000

        if self.transform is not None:
            audio = self.transform(audio)

        if self.target_transform is not None:
            target = self.target_transform(target)

        return audio, target

    def __len__(self):
        return len(self.splits[self.split])

    def set_split(self, s):
        self.split = s
    
    def find_max_len(self):
        self.maxlen = 0
        for fp in self.data:
            sig, sr = torchaudio.load(fp)
            self.maxlen = sig.size(0) if sig.size(0) > self.maxlen else self.maxlen

    def _set_lang(self, lang):
        try:
            self.url = self.URLS[lang]
            self.lang = lang
            self.langdir = os.path.join(self.basedir, "raw", lang)
            self.langzipsdir = os.path.join(self.basedir, "raw", "zips", lang)
            self.langfilelist = os.path.join(self.langzipsdir, "filelist")
            # make dirs
            _make_dir_iff(self.langzipsdir)
        except:
            print("{} is not a valid language selection".format(lang))


    def _get_source(self, url):
        response = requests.get(url)

        base_url = url.split('?')[0]

        if response.status_code == 200: return (base_url, response.status_code, response.text.replace('</html>', '') + '</html>')
        return (base_url, response.status_code, None)

    def _extract_links(self, source):
        soup = bs4.BeautifulSoup(source, "html5lib")

        links = soup.find_all('a')

        links = [a.get('href') for a in links][12:-1] # The beauty of static headers and footers
        if self.rand:
            random.shuffle(links)
        return links

    def _validate(self, links, allow_anon):
        # If a database of already downloaded files exists
        #  ..we just want to download new ones
        if allow_anon:
            return links
        else:
            return [link for link in links if not 'anonymous' in link]

    def _acquire(self, base_url, file_name):

        if os.path.isfile(os.path.join(self.langzipsdir, file_name)):
            return 1

        response = requests.get(base_url + file_name)

        if response.status_code == 200:
            # Download and save
            with open(os.path.join(self.langzipsdir, file_name), 'wb') as f:
                f.write(response.content)

            return 1
        return 0

    def batch_download(self, maximum=10, rand=False, allow_anon=False):
        # imports that are only needed to download

        for lang in self.langs:
            self._set_lang(lang)
            print(self.url)
            base_url, status, source = self._get_source(self.url)
            if status != 200:
                print('Could not connect to', self.url)
                break

            links = self._extract_links(source)
            if not len(links) > 0:
                print('No links found at url', url)

            to_download = self._validate(links, allow_anon)
            if not len(to_download) > 0:
                print('All available files have already been downloaded from', url)

            # Lets download
            counter = 0
            for link in to_download:
                result = self._acquire(base_url, link)

                counter += result

                if result==0: print('Unable to download', link)
                elif counter == maximum: break

    def extract_all(self):
        # import needed only for extracting zips
        import tarfile

        prompts = {}
        for lang in self.langs:
            prompts[lang] = {}
            self._set_lang(lang)
            existing = [f for f in os.listdir(self.langzipsdir) if os.path.isfile(os.path.join(self.langzipsdir, f))]
            for fp in existing:
                fp_noext = fp.rsplit(".", 1)[0]
                tardir = os.path.join(self.langzipsdir, fp_noext)
                if not os.path.isdir(tardir):
                    full_path = os.path.join(self.langzipsdir, fp)
                    # extract zip file
                    with tarfile.open(full_path) as zip_f:
                        zip_f.extractall(self.langzipsdir)
                # copy prompts
                promptdir = os.path.join(tardir, "etc")
                if os.path.isfile(os.path.join(promptdir, "prompts-original")):
                    promptfile = os.path.join(promptdir, "prompts-original")
                else:
                    promptfile = os.path.join(promptdir, "PROMPTS")
                prompts[lang][fp_noext] = {}
                with open(promptfile, "rb") as detect_f:
                    charset = chardet.detect(detect_f.read())
                    if "ISO" in charset["encoding"]:
                        encoding = charset["encoding"]
                    else:
                        encoding = None
                try:
                    with open(promptfile, "r", encoding=encoding) as prompt_f:
                        tmp_prompts = [line.strip().split(" ", 1) for line in prompt_f if len(line.strip()) > 0]
                        prompts[lang][fp_noext].update(dict(tmp_prompts))
                except Exception as e:
                    print(e)
                    print(tmp_prompts)
                # move audio files to destination folder
                if os.path.isdir(os.path.join(tardir, "flac")):
                    audiodir = os.path.join(tardir, "flac")
                elif os.path.isdir(os.path.join(tardir, "wav")):
                    audiodir = os.path.join(tardir, "wav")
                for audio_fp in os.listdir(audiodir):
                    audio_fp_name = os.path.basename(audio_fp)
                    src_file_path = os.path.join(audiodir, audio_fp_name)
                    dest_file_path = os.path.join(self.processeddir,
                                                  "audio",
                                                  "{}__{}__{}".format(self.lang,
                                                                    fp_noext,
                                                                    audio_fp_name)
                    )
                    audio_fp_noext = audio_fp_name.rsplit(".", 1)[0]
                    if not os.path.isfile(dest_file_path) and audio_fp_noext in prompts[lang][fp_noext]:
                        shutil.copyfile(src_file_path, dest_file_path)

        prompts = json.dumps(prompts, ensure_ascii=False, indent=4, sort_keys=True)
        dest_prompts_file = os.path.join(self.processeddir, "prompts", "prompts.json")
        with open(dest_prompts_file, "w", encoding='utf8') as json_f:
            json_f.write(prompts)

def _make_dir_iff(d):
    try:
        os.makedirs(os.path.join(d))
        print("{} created".format(d))
    except OSError as e:
        if e.errno == errno.EEXIST:
            pass
        else:
            raise