from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)

EVENTS = [
    {
        "id": 1,
        "name": "1회차 얼리버드 티켓팅",
        "open_at": NOW - timedelta(minutes=10),
    },
    {
        "id": 2,
        "name": "2회차 저녁 티켓팅",
        "open_at": NOW + timedelta(minutes=25),
    },
    {
        "id": 3,
        "name": "3회차 스페셜 티켓팅",
        "open_at": NOW + timedelta(minutes=70),
    },
]

SEAT_ROWS = ["A", "B", "C"]
SEAT_COLS = [1, 2, 3, 4]
SEAT_IDS = [f"{row}{col}" for row in SEAT_ROWS for col in SEAT_COLS]


def get_event(event_id: int) -> dict | None:
    for event in EVENTS:
        if event["id"] == event_id:
            return event
    return None
