# rhwp OLE 렌더링 패치

`rhwp_ole_render.patch` — rhwp 코어(rhwp-python 0.8.0 의 vendored `external/rhwp`, core 0.7.16)에
적용한 OLE 객체 렌더링 수정. 배포 wheel은
`../vendor/rhwp_python-0.8.0-cp310-abi3-manylinux_2_35_x86_64.whl`이다.

이 wheel의 패치된 `rhwp/_rhwp.abi3.so`는 기존 2.38 빌드와 바이트 단위로 동일하다
(SHA-256 `d0d7399310229382b4e5d7a148ba7b46f14a10560dd44040a9eb8f386ff037ba`).
번들 `libfontconfig`만 glibc 2.35 호환 빌드로 교체하고 wheel 태그와 `RECORD`를
다시 생성했다. Ubuntu 22.04/glibc 2.35에서 실제 HWP parse, PNG 렌더, SVG 렌더를
smoke test했다.

## 고치는 것 (공식 rhwp 대비)
- **ChemDraw 등 WMF 프리뷰 OLE**(화학구조식·수식): 빈 placeholder → 실제 렌더.
  `\x02OlePres000` 의 WMF(CF_METAFILEPICT) 를 기존 `convert_wmf_to_svg` 로 라우팅.
- **StaticDib OLE 비트맵**(보도자료 배너/로고 등): `CONTENTS` 스트림의 BMP/PNG/JPEG 를
  native image 로 렌더 (대소문자 무시 매칭 + 네이티브 좌표/네임스페이스 수정).
- **초대형 HWPX**(식품첨가물 기준규격 ~33MB, 의약품각조 ~44MB section.xml): `MAX_XML_SIZE`
  32MB→256MB 로 상향 (압축비 ~8–12x 정상 문서, GB급 폭탄은 여전히 차단).

미수정(알려진 한계): EMF 프리뷰 OLE 일부 — rhwp EMF 컨버터의 레코드 서브셋 한계.

## 재빌드 (참고)
```
pip download --no-binary :all: rhwp-python==0.8.0      # sdist
tar xzf rhwp_python-0.8.0.tar.gz && cd rhwp_python-0.8.0
patch -p1 < ../rhwp_ole_render.patch                   # external/rhwp 에 적용
maturin build --release --compatibility manylinux_2_35 # → target/wheels/*.whl
```
빌드엔 Rust 툴체인 + 시스템 freetype/fontconfig(개발 심볼릭)가 필요하다. wheel에
번들되는 모든 ELF의 최대 GLIBC 심볼이 2.35 이하인지 확인하고, glibc 2.35 노드에서
설치와 실제 렌더 smoke test를 통과시킨 뒤 `vendor/`에 반영한다.
