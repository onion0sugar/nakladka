-- ============================================================================
-- ZAPYTANIE O ZAMÓWIENIA — edytuj TEN plik, nie kod!
-- ============================================================================
-- Bot wykonuje to zapytanie co POLL_INTERVAL sekund. Jeśli zwróci wiersz —
-- wysyła powiadomienie na kanał Zello (nawet jeśli to ten sam wiersz, co
-- w poprzednim pollingu — bot nie pamięta obsłużonych zamówień).
--
-- Wymagania:
--   * max 1 wiersz (TOP 1),
--   * co najmniej 1 kolumna = numer zamówienia pokazywany w wiadomości
--     (u Ciebie: OriginalNumber),
--   * opcjonalnie druga kolumna `id` (liczba) — tylko do logów; wtedy
--     kolejność: id, numer.
--
-- Własny warunek wpisz w WHERE (np. status = 'oczekuje'), a kolumnę
-- sortowania / warunku dostosuj do swojej tabeli.
-- ============================================================================

SELECT DD.Id,
       DD.OriginalNumber,
       ZG.ZoneGroupId
  FROM [SerwisKop_Magazyn].[Document].[Documents] DD
  OUTER APPLY (
      SELECT TOP (1)
             ZGZ.ZoneGroupId
        FROM [SerwisKop_Magazyn].[Document].[DocumentPositions] DP
        LEFT JOIN [Stillage].[StillageSpaces] SS
          ON DP.FromStillageSpaceId = SS.Id
        LEFT JOIN [SerwisKop_Magazyn].[Zone].[ZoneGroupZones] ZGZ
          ON SS.ZoneId = ZGZ.ZoneId
       WHERE DP.DocumentId = DD.Id
       ORDER BY DP.Id
  ) ZG
  WHERE DocumentType = 7
  AND DD.DateCreatedUtc >= DATEADD(DAY, -30, GETUTCDATE())
  AND DocumentStatusText = 'in_progress'
  AND SubType = 50
