import ffmpeg, os, shutil
from dotenv import load_dotenv
from pathlib import Path
from typing import Optional, Tuple, Dict, Any
from mutagen import File as MutagenFile
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

def read_basic_tags(path: Path) -> Dict[str, Any]:

    def _first(v):
        """리스트/튜플이면 첫 원소, 아니면 그대로"""
        if isinstance(v, (list, tuple)):
            return v[0] if v else None
        return v

    def _parse_num_total(raw) -> Tuple[Optional[int], Optional[int]]:
        """
        '1/12', '01/12', '1', ('1','12'), ((1,12),) 등 다양한 표현을 (num,total)로 정규화
        """
        if raw is None:
            return None, None
        # MP4의 'trkn'/'disk'는 보통 [ (num, total) ] 형태
        if isinstance(raw, (list, tuple)) and raw and isinstance(raw[0], (list, tuple)):
            num, total = raw[0][0], raw[0][1] if len(raw[0]) > 1 else None
            return (int(num) if num else None, int(total) if total else None)

        raw = _first(raw)
        if raw is None:
            return None, None
        if isinstance(raw, (int, float)):
            return int(raw), None

        s = str(raw).strip()
        if "/" in s:
            a, b = s.split("/", 1)
            a = a.strip() or None
            b = b.strip() or None
            return (int(a) if a and a.isdigit() else _safe_int(a),
                    int(b) if b and b.isdigit() else _safe_int(b))
        # 단일 숫자
        return (_safe_int(s), None)

    def _safe_int(x) -> Optional[int]:
        try:
            return int(str(x).strip())
        except Exception:
            return None
    audio = MutagenFile(path)
    if audio is None:
        raise ValueError(f"지원되지 않는 파일이거나 손상된 파일: {path}")

    out = {
        "albumartist": None,
        "album": None,
        "disc": None,
        "disc_total": None,
        "track": None,
        "track_total": None,
        "artist": None,
        "title": None,
        "year": None,
    }

    # --- FLAC ---
    if isinstance(audio, FLAC):
        tags = audio.tags or {}
        get = lambda k: _first(tags.get(k))
        out["albumartist"] = get("albumartist") or get("ALBUMARTIST")
        out["album"]       = get("album") or get("ALBUM")
        d, dt = _parse_num_total(get("discnumber") or get("DISCNUMBER"))
        out["disc"], out["disc_total"] = d, dt
        t, tt = _parse_num_total(get("tracknumber") or get("TRACKNUMBER"))
        out["track"], out["track_total"] = t, tt
        out["artist"]     = get("artist") or get("ARTIST")
        out["title"]      = get("title") or get("TITLE")
        # 발매 연도: 'date' 또는 'year'
        date_str = get("date") or get("year")
        if date_str:
            out["year"] = _safe_int(str(date_str)[:4])  # YYYY-MM-DD 중 앞 4자리
        return out

    # --- MP4 / M4A ---
    if isinstance(audio, MP4):
        tags = audio.tags or {}
        out["albumartist"] = _first(tags.get("aART"))
        out["album"]       = _first(tags.get("\xa9alb"))
        d, dt = _parse_num_total(tags.get("disk"))
        out["disc"], out["disc_total"] = d, dt
        t, tt = _parse_num_total(tags.get("trkn"))
        out["track"], out["track_total"] = t, tt
        out["artist"]     = _first(tags.get("\xa9ART"))
        out["title"]      = _first(tags.get("\xa9nam"))
        # 연도: '©day'
        date_str = _first(tags.get("\xa9day"))
        if date_str:
            out["year"] = _safe_int(str(date_str)[:4])
        return out

    # --- MP3 / ID3 ---
    if isinstance(audio, ID3) or hasattr(audio, "tags") and isinstance(audio.tags, ID3):
        id3 = audio if isinstance(audio, ID3) else audio.tags
        albumartist = None
        if "TPE2" in id3:
            albumartist = id3["TPE2"].text[0]
        if not albumartist and "TXXX:ALBUMARTIST" in id3:
            albumartist = id3["TXXX:ALBUMARTIST"].text[0]

        out["albumartist"] = albumartist
        out["album"]       = id3["TALB"].text[0] if "TALB" in id3 else None
        d, dt = _parse_num_total(id3["TPOS"].text[0] if "TPOS" in id3 else None)
        out["disc"], out["disc_total"] = d, dt
        t, tt = _parse_num_total(id3["TRCK"].text[0] if "TRCK" in id3 else None)
        out["track"], out["track_total"] = t, tt
        out["artist"]     = id3["TPE1"].text[0] if "TPE1" in id3 else None
        out["title"]      = id3["TIT2"].text[0] if "TIT2" in id3 else None
        # 연도: TDRC(Recording time) 또는 TYER
        if "TDRC" in id3:
            date_str = str(id3["TDRC"].text[0])
        elif "TYER" in id3:
            date_str = str(id3["TYER"].text[0])
        else:
            date_str = None
        if date_str:
            out["year"] = _safe_int(date_str[:4])
        return out

    # --- 그 외 포맷 ---
    tags = getattr(audio, "tags", None) or {}
    out["albumartist"] = _first(tags.get("albumartist"))
    out["album"]       = _first(tags.get("album"))
    d, dt = _parse_num_total(tags.get("discnumber") or tags.get("disc"))
    out["disc"], out["disc_total"] = d, dt
    t, tt = _parse_num_total(tags.get("tracknumber") or tags.get("track"))
    out["track"], out["track_total"] = t, tt
    out["artist"]     = _first(tags.get("artist"))
    out["title"]      = _first(tags.get("title"))
    date_str = _first(tags.get("date") or tags.get("year"))
    if date_str:
        out["year"] = _safe_int(str(date_str)[:4])

    return out

def flac_to_alac_fp(src: Path, dst: Path):
    """
    FLAC → ALAC(.m4a) 변환 (무손실 + 메타데이터/커버/챕터 보존)
    """
    dst = dst.with_suffix(".m4a")
    dst.parent.mkdir(parents=True, exist_ok=True)

    (
        ffmpeg
        .input(str(src))
        .output(
            str(dst),
            **{
                "c:a": "alac",            # 오디오: ALAC 무손실 인코딩
                "c:v": "copy",            # 커버아트 그대로 복사
                "disposition:v": "attached_pic",  # 커버아트를 '첨부 그림'으로 표시
                "map": "0",               # 원본의 모든 스트림 매핑
                "map_metadata": "0",      # 모든 메타데이터 복사
                "map_chapters": "0",      # 챕터 복사
                "movflags": "use_metadata_tags",  # m4a 태그로 반영
            }
        )
        .overwrite_output()  # 이미 존재하면 덮어쓰기
        .run(quiet=False)
    )

def main():

    input_path = Path(input('Enter Input Path'))
    output_path = Path(os.path.join(str(os.getenv('OUTPUT_PATH')), input('Enter Output Subfolder name')) if os.getenv('OUTPUT_PATH') else input('Enter Output Path'))

    # 찾고 싶은 확장자 목록
    exts = {".flac", ".wav", ".alac", ".m4a", ".mp3", ".aac"}

    # 재귀적으로 모든 하위 폴더 탐색
    all_audio_file_list = [p for p in input_path.rglob("*") if p.suffix.lower() in exts]

    for each_audio_file in all_audio_file_list:

        audio_file_tags = read_basic_tags(each_audio_file)

        audio_dirname = f'{audio_file_tags["albumartist"]} - {audio_file_tags["album"]} ({audio_file_tags["year"]})'
        audio_basename = f'{audio_file_tags["disc"]} - {audio_file_tags["track"]} - {audio_file_tags["title"]}'

        os.makedirs(os.path.join(output_path, audio_dirname), exist_ok=True)

        if each_audio_file.suffix == '.flac':

            input_filename = each_audio_file
            output_filename = Path(os.path.join(audio_dirname, f'{audio_basename}.alac'))

            flac_to_alac_fp(input_filename, output_filename)

        else:

            input_filename = each_audio_file
            output_filename = Path(os.path.join(audio_dirname, f'{audio_basename}{each_audio_file.suffix}'))

            shutil.copy2(input_filename, output_filename)

    print('Finished!')

if __name__ == "__main__":
    main()

