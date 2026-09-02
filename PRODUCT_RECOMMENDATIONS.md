# 메디톡 제품 기능 제안

현재 메디톡은 기사 작성·심사·발행, 예약 발행, 검색, 회원, 댓글, 스크랩, 뉴스레터, 통계, RSS와 광고 슬롯을 갖춘 상태입니다. 다음 단계에서는 기능 수를 늘리기보다 **편집 품질, 독자 재방문, 광고 운영 데이터**를 강화하는 순서가 적합합니다. 뉴스 조직의 제품 개발은 편집 판단과 독자 행동 데이터를 함께 고려해야 하며[1], 현대 뉴스 CMS는 역할 기반 워크플로우, 버전 관리, 다채널 발행, SEO, 실시간 분석을 핵심 역량으로 봅니다.[2]

| 우선순위 | 제안 기능 | 메디톡에 필요한 이유 | 구현 범위 |
|---|---|---|---|
| 1 | 기사 수정 이력·복원 | 제약·규제 기사는 정정 과정과 책임 소재가 중요합니다. 편집장 승인 전후 버전을 비교하고 이전 버전으로 되돌릴 수 있어야 합니다. | 기사 버전 테이블, 변경 diff, 복원 버튼 |
| 2 | 기사별 성과 대시보드 | 단순 조회수보다 유입 경로, 평균 읽기 시간, 재방문, 스크랩을 함께 보면 편집 판단과 광고 영업에 도움이 됩니다. FT Strategies도 페이지뷰 외에 체류시간·재방문 같은 참여 지표를 강조합니다.[3] | 기사별 조회·스크랩·댓글·유입 채널 카드 |
| 3 | 주제·기업 팔로우 | 독자가 기업명, 성분명, 임상 단계 같은 관심 주제를 저장하면 전문지의 재방문 이유가 생깁니다. | 관심 키워드 저장, 마이페이지 피드, 선택 알림 |
| 4 | 데일리 브리핑·분야별 뉴스레터 | 홈페이지 직접 방문이 줄고 뉴스레터·메신저 같은 경로가 중요해지는 흐름에 대응합니다.[3] | 임상·정책·M&A 구독 분리, 발송 이력 |
| 5 | 광고 성과·기간 관리 | 현재 추가한 슬롯에 노출 기간, 클릭 수, 광고주별 캠페인 상태를 더하면 실제 영업 관리가 쉬워집니다. | 시작·종료일, 노출·클릭 카운트, 캠페인 보고서 |
| 6 | 기사 체크리스트 | 발행 전 출처·이해상충·제목·SEO·이미지 저작권을 점검해 오류를 줄입니다. 역할 기반의 감사 가능한 워크플로우는 현대 뉴스 CMS의 핵심 요소입니다.[2] | 발행 전 필수 체크, 누락 경고, 감사 로그 |
| 7 | 데이터·문서 첨부형 기사 | 공시, 허가 문서, 임상 표와 그래프를 본문에 구조적으로 붙이면 전문 매체의 차별점이 커집니다. FT Strategies는 데이터 저널리즘·그래픽·멀티미디어 역량을 강조합니다.[3] | PDF 원문 링크, 표·차트 블록, 출처 박스 |

가장 먼저 권하는 조합은 **기사 수정 이력 + 기사별 성과 대시보드 + 주제 팔로우**입니다. 이 세 기능은 편집 신뢰도, 독자 충성도, 광고 영업 근거를 각각 강화하면서 현재 시스템과도 자연스럽게 연결됩니다.

## References

[1]: https://newsproduct.org/product-kit/understanding-the-role-of-product-in-news-organizations "News Product Alliance — Understanding the role of product in news organizations"
[2]: https://www.brightspot.com/cms-resources/cms-insights/essential-cms-features-for-news-media-publishers "Brightspot — Essential CMS features for news media publishers"
[3]: https://www.ftstrategies.com/en-gb/insights/designing-the-newsroom-of-the-future "FT Strategies — Designing the newsroom of the future"

