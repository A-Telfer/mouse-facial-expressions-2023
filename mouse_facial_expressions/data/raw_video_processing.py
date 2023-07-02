"""
1. Rename and crop videos
2. Extract deeplabcut labels
"""
import logging
import os
import subprocess
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from tqdm import tqdm
from PIL import Image

import click
import cv2
import pandas as pd
import numpy as np
from dotenv import find_dotenv, load_dotenv

project_dir = Path(__file__).resolve().parents[2]
load_dotenv(find_dotenv())

def variance_of_laplacian(image):
    """Used for blur detection"""
    return cv2.Laplacian(image, cv2.CV_64F).var()

def get_meta_csv():
    project_dir = Path(__file__).resolve().parents[2]
    f = project_dir / f'data/raw/raw_videos_{os.environ.get("MFE_VERSION")}.csv'
    return str(f)


def get_raw_video_folder():
    try:
        f = Path(os.environ["MFE_RAW_VIDEO_FOLDER"])
        return str(f)
    except:
        return None


def get_processed_video_folder():
    try:
        f = Path(os.environ["MFE_PROCESSED_VIDEO_FOLDER"])
        f = f / os.environ.get("MFE_VERSION")
        return str(f)
    except:
        return None


def get_dlc_facial_labels_folder():
    try:
        f = Path(os.environ["MFE_DLC_FACIAL_LABELS_FOLDER"])
        f = f / os.environ.get("MFE_VERSION")
        return str(f)
    except:
        return None


def get_dlc_facial_project_folder():
    try:
        return os.environ["MFE_DLC_FACIAL_PROJECT_PATH"]
    except:
        return None


def get_extracted_frames_folder():
    try:
        f = Path(os.environ["MFE_EXTRACTED_FRAMES_FOLDER"])
        f = f / os.environ.get("MFE_VERSION")
        return str(f)
    except:
        return None


def to_seconds(time_str):
    d = datetime.strptime(time_str, "%M:%S")
    d = timedelta(minutes=d.minute, seconds=d.second)
    return int(d.total_seconds())


def seconds_to_str(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{minutes:02}:{seconds:02}"


def find_video_from_details(row, video_directory):
    s = f"{row.camera}__*__{row.year:04}{row.month:02}{row.day:02}_{row.hour:02}{row.minutes:02}{row.seconds:02}*.mp4"
    try:
        return next(video_directory.glob(s)).parts[-1]
    except:
        return ""


def get_angle_between_bodyparts(df, bp1, bp2):
    bodyparts_df = df.droplevel(0, axis=1)
    deltas = bodyparts_df.loc[:, bp2] - bodyparts_df.loc[:, bp1]
    angles = np.arctan2(deltas.y, deltas.x)
    return np.rad2deg(angles)


def preprocess_video(row, input_directory, output_directory):
    if row.discard == 1:
        return

    cmd = "ffmpeg -hide_banner -loglevel error -y"

    # Add a start flag if start time is specified
    if row.start != "0" and row.start != "nan":
        cmd += f" -ss {row.start}"

    # Add a duration flag if end time is specified
    if row.end != "-1" and row.end != "nan":
        if row.start != "0" and row.start != "nan":
            duration = seconds_to_str(to_seconds(row.end) - to_seconds(row.start))
        else:
            duration = row.end

        cmd += f" -t {duration}"

    # Add the video in
    cmd += f' -i "{str(input_directory / row.original_video)}"'

    # Specify codec
    cmd += " -c copy"

    # Output file
    recording_labels = dict(
        enumerate(
            [
                "rec0_acclimation",
                "rec1_preinjection",
                "rec2_1h-postinjection",
                "rec3_2h-postinjection",
                "rec4_4h-postinjection",
            ]
        )
    )
    fname = f"{row.animal}_{recording_labels[int(row.recording)]}.mp4"
    cmd += f' "{output_directory / fname}"'

    return cmd


@click.group()
def main():
    pass


class FrameExtractor:
    def __init__(self, video, dlc_file, pcutoff=0.6, padding=150):
        self.pcutoff = pcutoff
        self.video = str(video)
        self.dlc_file = str(dlc_file)
        self.padding = padding

        self.cap = cv2.VideoCapture(self.video)
        self.df = pd.read_hdf(self.dlc_file)

        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.nframes = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        assert self.nframes == self.df.shape[0]

        # Only applies to rotation
        self.fill = tuple(np.array([128, 128, 128], dtype=np.uint8))

        # Compute
        self.compute_rotations_and_sides()
        self.compute_visibilities()

    def compute_visibilities(self):
        likelihood = self.df.xs("likelihood", axis=1, level="coords").droplevel(0, axis=1)
        visibility = likelihood > self.pcutoff
        self.left_side_visible = visibility.nose & visibility.left_eye & visibility.left_ear
        self.right_side_visible = visibility.nose & visibility.right_eye & visibility.right_ear

    def compute_rotations_and_sides(self):
        self.left_angles = get_angle_between_bodyparts(self.df, "left_eye", "left_ear")
        self.right_angles = get_angle_between_bodyparts(self.df, "right_eye", "right_ear")

        # centre point to perform rotations and crop around
        self.right_side_centre = (
            self.df.loc[:, pd.IndexSlice[:, ["nose", "right_eye", "right_ear"]]]
            .groupby(level="coords", axis=1)
            .mean()
        )
        self.left_side_centre = (
            self.df.loc[:, pd.IndexSlice[:, ["nose", "left_eye", "left_ear"]]]
            .groupby(level="coords", axis=1)
            .mean()
        )

    def __getitem__(self, idx):
        # Get frame
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = self.cap.read()
        image = Image.fromarray(frame)

        # Select which side is being faced
        if self.right_side_visible.loc[idx]:
            centre = self.right_side_centre
            angles = self.right_angles
        else:
            centre = self.left_side_centre
            angles = self.left_angles

        # Rotate around centre
        x, y = centre.loc[idx, ["x", "y"]]
        image = image.rotate(angles.loc[idx], center=(x, y), fillcolor=self.fill)

        # Crop
        xmin, xmax = x - self.padding, x + self.padding
        ymin, ymax = y - self.padding, y + self.padding
        image = image.crop((xmin, ymin, xmax, ymax))
        return image

    def __len__(self):
        return self.nframes


@main.command()
@click.option("--nframes", default=1000, type=int)
@click.option("--sample_every", default=100, type=int)
@click.option("--skip_existing", default=True, type=bool)
@click.option("--processed_videos_folder", default=get_processed_video_folder(), type=click.Path())
@click.option(
    "--dlc_facial_labels_folder", default=get_dlc_facial_labels_folder(), type=click.Path()
)
@click.option(
    "--extracted_frames_folder", default=get_dlc_facial_labels_folder(), type=click.Path()
)
def extract_frames(
    nframes,
    sample_every,
    skip_existing,
    processed_videos_folder,
    dlc_facial_labels_folder,
    extracted_frames_folder,
):
    logger = logging.getLogger(__name__)
    logger.info("Extracting frames from videos")

    processed_videos_folder = Path(processed_videos_folder)
    dlc_facial_labels_folder = Path(dlc_facial_labels_folder)
    extracted_frames_folder = Path(extracted_frames_folder)

    assert processed_videos_folder.exists()
    assert dlc_facial_labels_folder.exists()
    if not extracted_frames_folder.exists():
        extracted_frames_folder.mkdir(parents=True)

    logger.info("Matching videos and DLC files")
    dlc_files = list(dlc_facial_labels_folder.glob("*.h5"))
    data = []
    for dlc_file in tqdm(dlc_files, leave=False):
        fname, _ = dlc_file.parts[-1].split("DLC")
        video = processed_videos_folder / f"{fname}.mp4"
        data.append(dict(video=video, dlc_file=dlc_file, fname=fname))
    files_df = pd.DataFrame(data)

    for idx, row in files_df.iterrows():
        logger.info("Processing video %s", row.video.parts[-1])

        video_extracted_frames_folder = extracted_frames_folder / row.fname
        if video_extracted_frames_folder.exists() and skip_existing:
            logger.info("Video folder already exists, skipping")
            continue
        elif not video_extracted_frames_folder.exists():
            video_extracted_frames_folder.mkdir(parents=True)

        logger.info("Sampling portrait frames")
        frame_extractor = FrameExtractor(row.video, row.dlc_file)
        is_side_portrait = frame_extractor.right_side_visible ^ frame_extractor.left_side_visible
        data = []
        for i in tqdm(np.arange(0,len(frame_extractor), sample_every), leave=False, desc="Frame"):
            
            image = frame_extractor[i]
            blur = variance_of_laplacian(image)
            data.append(dict(blur=blur, frame=i))
        blur_df = pd.DataFrame(data)

        logger.info("Sorting by blur")
        blur_df = blur_df.sort_values('blur', ascending=False) # Sort so higher values are first

        logger.info("Saving top %i frames", nframes)
        blur_df = blur_df.head(nframes)
        for idx, row in blur_df.iterrows():
            filepath = video_extracted_frames_folder / f"frame{row.frame:05}.png"
            image.save(filepath)

        logger.info("Extracting frames from video complete")

    logger.info("Extracting all frames complete")

@main.command()
@click.option("--input_folder", default=get_processed_video_folder(), type=click.Path())
def check_videos_loadable(input_folder):
    logger = logging.getLogger(__name__)
    logger.info("checking to see if videos can be loaded")

    # Setup file paths
    error_videos = []
    input_folder = Path(input_folder)
    videos = list(map(str, input_folder.glob("*.mp4")))
    for video in videos:
        try:
            cap = cv2.VideoCapture(video)
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            assert frames > 0
            ret, frame = cap.read()
            assert ret
            logger.info("Video %s contained %i frames", Path(video).parts[-1], frames)
        except:
            error_videos.append(video)

    logger.info(
        "check complete with %i/%i successful", len(videos) - len(error_videos), len(videos)
    )
    for video in error_videos:
        logger.info("Problem loading: %s", video)


@main.command()
def list_raw_videos():
    logger = logging.getLogger(__name__)
    logger.info("making final data set from raw data")

    # Setup file paths
    project_dir = Path(__file__).resolve().parents[2]
    meta_df = pd.read_csv(project_dir / "data/raw/raw_videos_20230627.csv")
    meta_df["recording"] = meta_df["recording"].astype(str)
    for group, group_df in meta_df.groupby("animal"):
        print(f"{group}: " + ", ".join(group_df.recording.tolist()))


@main.command()
@click.option("--splits", default=None, type=int)
@click.option("--split_index", default=None, type=int, help="1 to splits-1")
@click.option("--dlc_project", default=get_dlc_facial_project_folder(), type=click.Path())
@click.option("--input_folder", default=get_processed_video_folder(), type=click.Path())
@click.option("--output_folder", default=get_dlc_facial_labels_folder(), type=click.Path())
def dlc_process_videos(splits, split_index, dlc_project, input_folder, output_folder):
    logger = logging.getLogger(__name__)
    logger.info("labeling deeplabcut videos")
    import deeplabcut

    output_folder = Path(output_folder)
    if not output_folder.exists():
        output_folder.mkdir(parents=True)
        logger.info("Created new output folder %s", output_folder)
    else:
        logger.info("Output folder exists, reusing %s", output_folder)

    # Setup file paths
    video_dir = Path(input_folder)
    videos = list(video_dir.glob("*.mp4")) + list(video_dir.glob("*.MP4"))
    videos = sorted(videos)
    videos = list(map(str, videos))

    if splits is not None and split_index is not None:
        logger.info("Splits provided %i/%i", split_index, splits)
        assert split_index >= 0 and split_index < splits
        start = len(videos) // splits * split_index
        if split_index == splits - 1:
            end = -1
        else:
            end = len(videos) // splits * (split_index + 1)

        logger.info("Splitting from video %i to %i", start, end)
        videos = videos[start:end]

    # DLC
    config = Path(dlc_project) / "config.yaml"
    deeplabcut.analyze_videos(str(config), videos, destfolder=str(output_folder))
    deeplabcut.create_labeled_video(str(config), videos, destfolder=str(output_folder))


@main.command()
@click.option("--input_folder", default=get_raw_video_folder())
@click.option("--output_folder", default=get_processed_video_folder())
@click.option("--meta_csv", default=get_meta_csv())
@click.option("-m", "--mouse", multiple=True, default=None, help="e.g. `m1.0, m2.2, f16.2`")
def rename(input_folder, output_folder, meta_csv, mouse):
    """Runs data processing scripts to turn raw data from (../raw) into
    cleaned data ready to be analyzed (saved in ../processed).
    """
    logger = logging.getLogger(__name__)
    logger.info("making final data set from raw data")

    # Setup file paths
    project_dir = Path(__file__).resolve().parents[2]
    input_video_folder = Path(input_folder)
    output_folder = Path(output_folder)
    meta_df = pd.read_csv(meta_csv)

    if output_folder is None:
        output_folder = Path(os.environ["MFE_PROCESSED_VIDEOS"])
    else:
        output_folder = Path(output_folder)
        if not output_folder.exists():
            output_folder.mkdir(parents=True)

    # Match the meta info file to the raw video filenames
    logger.info("Finding video names from the meta information file")
    meta_df["original_video"] = meta_df.apply(
        lambda x: find_video_from_details(x, video_directory=input_video_folder), axis=1
    )

    # Clean up datatypes
    meta_df["start"] = meta_df["start"].astype(str)
    meta_df["end"] = meta_df["end"].astype(str)
    meta_df["recording"] = meta_df["recording"]

    if not output_folder.exists():
        logger.info(f"Output directory not found, creating directory '{output_folder}'")
        output_folder.mkdir(parents=True)

    # if specific mice were specified, print them out
    if len(mouse):
        logger.info("Individual mice specified")
        rows = []
        for m in mouse:
            try:
                id, rec = m.split(".")
                rec = int(rec)
                row = meta_df[
                    (meta_df.animal == id) & (meta_df.recording == rec) & (meta_df.discard != 1)
                ].iloc[0]
                rows.append(row)
            except:
                logger.warn(f"Mouse {id} and recording {rec} are not in dataset")

        meta_df = pd.DataFrame(rows)

    # process videos
    for idx, row in meta_df.iterrows():
        if str(row.discard) == "1":
            video = find_video_from_details(row, input_video_folder)
            logger.info(f"Video marked for discard: {video}")
            continue

        cmd = preprocess_video(row, input_video_folder, output_folder)
        if cmd:
            logger.info(f"Processing video with command: `{cmd}`")
            process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
            process.wait()
        else:
            logger.info(f"Skipping video: `{row.original_video}`")

    logger.info("Processing data")


if __name__ == "__main__":
    log_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_fmt)
    main()
