# Cooper: 105 multi-constraint test prompts

Each prompt combines 4 or more constraints: a ranking (top/bottom N), one or more metrics, a period and a comparison period, filters (zone, region, state, brand, category), and a condition (grew by more than X%, fell, flat, below average). The first one is the North zone version of the question Cooper failed on. Deoleo has no zone called North1, so the prompt uses North. The last 5 are multi-line and Hinglish/Hindi versions.

The same list is in the repo as `backend/tests/fixtures/test_sheet_analyst.json`.


## Sales

1. Compare the sales performance of top 10 distributors in the North zone over the last 90 days against the previous 90 days. Identify distributors where primary sales grew by more than 15%, but secondary sales declined or remained flat.
2. Show the bottom 10 distributors in Maharashtra by secondary sales for last month. For each, give primary sales, secondary sales, the gap between them, and how both changed versus the month before.
3. Which top 5 brands grew secondary sales by more than 20% in Q2 compared with Q1, and in which zones did each of them decline?
4. For the top 10 distributors by primary sales this financial year, show month-wise primary sales from April to September and flag any distributor whose billing fell in 2 or more consecutive months.
5. List distributors in the South zone whose primary sales in the last 60 days are above ₹10 lakh but whose secondary sales are less than 70% of primary.
6. Compare MTD secondary sales against LMTD for every region in the East zone, show the % change, and highlight regions that dropped more than 10%.
7. Top 5 categories by secondary sales in Karnataka for September vs August. Which category gained the most share and which one lost the most?
8. Show the bottom 10 SKUs by secondary sales value in the last 30 days among SKUs that sold at least once in each of the previous 3 months.
9. Give me zone-wise primary sales for the last 3 months, the month-on-month growth for each, and the zone with the highest and lowest growth.
10. Which distributors in UP billed nothing in the last 15 days but billed more than ₹2 lakh in the 30 days before that?
11. Compare Figaro and Bertolli secondary sales by zone for the last 90 days vs the previous 90 days. Where is Figaro growing faster than Bertolli?
12. Top 10 cities by secondary sales this quarter. For each, show order value, number of orders, drop size, and the change in drop size versus last quarter.
13. Show distributors whose primary sales this month are more than 25% below their average monthly primary sales of the last 6 months, sorted by the size of the drop.
14. For the Pure category, show region-wise secondary sales for the last 4 weeks, week by week, and point out regions where sales fell for 3 weeks in a row.
15. Which top 5 regions contributed most to the growth in secondary sales from July to September, and what share of the total growth did each contribute?
16. Compare primary invoice value and secondary invoice value for each zone between 1 Oct and 23 Oct this year and the same dates last year.
17. Bottom 5 brands by secondary sales growth in the West zone for the last 2 months vs the 2 months before, only brands with sales above ₹5 lakh in the earlier period.
18. Show me the top 10 distributors where primary sales grew more than 30% this quarter but the number of outlets they serve went down.
19. Compare weekday versus Sunday secondary sales for the last 8 weeks for each zone, and tell me which zone depends most on weekend sales.
20. For distributors in Tamil Nadu and Kerala, show primary sales and secondary sales for last month and list the ones where secondary is higher than primary by more than 10%.

## Productivity

21. Show the bottom 10 sales reps by PC% in the North zone for the last 30 days, only reps with at least 200 total calls, and compare their PC% with the previous 30 days.
22. Which top 5 regions had the highest productive calls this week vs last week, and which regions saw PC drop more than 15%?
23. Compare TC, PC, PC%, and lines per call for each zone for September vs August, and flag zones where TC went up but PC% went down.
24. List reps in Maharashtra whose average calls per day are below 25 in the last 2 weeks and whose PC% is below the zone average.
25. Top 10 reps by order value in the last 60 days. For each, show TC, PC, PC%, drop size, and lines per call, and highlight anyone whose drop size fell by more than 20%.
26. Bottom 5 regions by lines per call in Q2, with their LPC in Q1, and the change in percentage points.
27. Show PC trend week by week for the last 8 weeks for the East zone, and tell me in which week PC dropped the most and why.
28. Which reps made more than 40 calls a day on average last month but converted less than 15% of calls into orders?
29. Compare productive calls per rep per day across all zones for the last 90 days vs the previous 90 days, ranked from best to worst improvement.
30. Show the top 10 territories by PC% this month, only territories with at least 5 reps, and their PC% last month.
31. Compare drop size for the Figaro brand across regions for last month vs the same month last year, showing only regions where drop size fell.
32. Give me reps whose total calls fell by more than 30% this week compared with their average week over the last 4 weeks.
33. Show the bottom 10 reps by order value per productive call in South zone for October so far, and compare with September.
34. For each zone, show average calls per rep, PC%, and drop size for the last 30 days, and rank zones by overall productivity.
35. Which top 5 regions improved PC% by more than 3 percentage points this month vs last month, and how did their total calls change?
36. List reps in UP with PC% above 30% but drop size below ₹500 in the last 45 days.
37. Compare productive calls on the first 10 days of October with the first 10 days of September for each region, and highlight regions with a drop of over 20%.
38. Show the top 10 reps by UBO this month and their UBO last month, and list any rep whose UBO fell by more than 25%.
39. Which zone has the biggest gap between total calls and productive calls in the last 2 weeks, and which 5 reps in that zone have the lowest PC%?
40. Give the month-wise trend of lines per call and drop size for the last 6 months for Karnataka and Tamil Nadu side by side.

## Discipline

41. Show the bottom 10 reps by average first call time in the North zone for the last 30 days, only reps who worked at least 15 days, and compare with the previous 30 days.
42. Which reps started their first call after 11 AM on more than 5 days in the last 2 weeks, and what was their PC% on those days?
43. Compare average call start time by zone for this month vs last month and flag zones where the start time moved later by more than 20 minutes.
44. List reps who were on leave or absent for more than 4 days in October, along with their productive calls and order value for the month.
45. Show the top 10 reps with the most zero-order days (calls but no orders) in the last 30 days, and their total calls on those days.
46. How many market days did each zone have this week vs last week, and how much of the PC drop is explained by fewer working days?
47. Show reps in Maharashtra who followed their tour plan on fewer than 80% of days last month, ranked from lowest adherence.
48. Compare average time spent in market per rep by region for the last 4 weeks vs the 4 weeks before, and list the bottom 5 regions.
49. Which reps had fewer than 20 working days in September but more than 22 working days in August, and how did their sales change?
50. Show reps whose last call of the day was before 4 PM on more than 30% of days this month, with their average calls per day.
51. For the East zone, show day-wise attendance (reps who started their day) for the last 14 days and point out days with more than 20% reps missing.
52. Bottom 10 reps by number of days with at least 25 calls in October, only reps who worked at least 15 days.
53. Compare the share of reps starting their day before 9:30 AM across zones for the last 30 days vs the previous 30 days.
54. List reps who made fewer than 10 calls on more than 3 working days in the last 2 weeks, along with their region and manager.
55. Which regions had the most holiday and leave days marked in October, and how did that affect productive calls compared with September?

## Distribution & outlets

56. Show the bottom 5 zones by coverage % for the last 30 days, and for each, the number of outlets visited, total outlets, and the change in coverage vs the previous 30 days.
57. Which top 10 cities have the highest number of inactive outlets this quarter, and what % of their outlet base is that?
58. Compare UBO and UBO % for each region for September vs August, and list regions where UBO grew but UBO % fell.
59. List distributors in Karnataka whose number of billed outlets fell by more than 20% in the last 60 days vs the 60 days before.
60. Show new outlets added by each zone month by month for the last 6 months, and the % of new outlets that ordered at least once.
61. Top 5 regions by penetration % for the EV category this quarter, and the same regions' penetration last quarter.
62. Which categories have penetration below 20% in the South zone this month, and what was their penetration 3 months ago?
63. Show the bottom 10 territories by active outlets in October so far, only territories with more than 300 outlets in total.
64. Compare coverage %, UBO %, and PC% for each zone in the last 30 days and identify zones that are weak on all three.
65. How many outlets ordered Figaro Baby in the last 90 days vs the previous 90 days in each region, and where did it lose the most outlets?
66. Show distributors serving more than 200 outlets where less than 40% of outlets billed in the last 30 days.
67. Which top 10 cities added the most new outlets in Q2, and how many of those new outlets are still active this month?
68. Compare outlet coverage for weekdays vs Sundays for the last 4 weeks in Maharashtra and Gujarat.
69. Show region-wise inactive outlets that were active last month, and the secondary sales lost from them compared with last month.
70. For the Lotion and Massage Oil categories, show UBO by zone for the last 60 days and the zones where UBO fell more than 15% vs the previous 60 days.

## Cross-metric diagnostics

71. Identify distributors where primary sales grew by more than 20% in the last quarter but secondary sales grew by less than 5%, which may mean stock is building up. Show the top 10 by the gap.
72. For the top 5 zones by secondary sales, compare PC%, drop size, and coverage % for this month vs last month, and say which metric drove the change in each zone.
73. Show the top 10 reps by sales growth in the last 30 days vs the previous 30 days, and for each, whether the growth came from more calls, a better PC%, or a bigger drop size.
74. Which brands have high sales growth (above 15%) but falling UBO in the last 60 days vs the 60 days before, by zone?
75. Compare the bottom 10 reps by PC with the top 10 reps by PC this month on average first call time, calls per day, and lines per call.
76. Find regions where the number of reps went down this quarter but secondary sales still grew by more than 10%, and show sales per rep.
77. List distributors whose primary sales fell more than 15% in October so far vs September, and show whether their outlets' orders also fell.
78. Show zones where total calls increased by more than 10% this month but order value decreased, with the change in drop size and PC%.
79. For the bottom 5 regions by UBO % in the last 30 days, show coverage %, PC%, and average calls per rep to explain why UBO is low.
80. Compare primary sales, secondary sales, PC, and UBO for each zone for the last 90 days against the previous 90 days in a single table, and rank zones by overall improvement.
81. Which top 5 categories have the biggest difference between their share of primary sales and their share of secondary sales this quarter?
82. Identify reps whose PC% improved by more than 5 points but whose order value fell in the last 30 days vs the previous 30 days.
83. Show distributors in the West zone with the highest secondary sales per outlet last month, only distributors with more than 100 outlets, and their primary sales growth.
84. Compare new outlet additions and inactive outlets for each region this quarter, and show regions where we lost more outlets than we added.
85. For the top 10 distributors by secondary sales, show this month vs last month for secondary sales, billed outlets, and average bill value, and flag any distributor where all three fell.

## Why & where (root cause)

86. Why did primary sales in the North zone fall in the last 30 days compared with the previous 30 days, and which 5 distributors caused most of the drop?
87. Why is PC% low in Maharashtra this week? Break it down by region and show the 5 reps with the biggest drop vs last week.
88. Where did secondary sales of Bertolli drop the most last month vs the month before? Show the bottom 5 regions and the bottom 5 distributors.
89. Why did UBO fall in the South zone in October so far compared with the same days in September, and what should we do about it?
90. What are the bottom 10 reps in the East zone doing wrong this month compared with the top 10 reps? Compare first call time, calls per day, PC%, and drop size.
91. Why did coverage drop in Karnataka over the last 2 weeks vs the 2 weeks before, and which territories are responsible?
92. Which zones and regions explain the overall 10% fall in productive calls this week, and how much of it was due to the holiday?
93. Why is the West zone's drop size lower this quarter than last quarter? Show the categories and reps behind it.
94. Where are we losing outlets? Show regions with the biggest increase in inactive outlets this month vs last month and the top 5 distributors in each.
95. Why did secondary sales grow in UP last month while primary sales fell, and which distributors show this pattern?
96. Why are total calls down in the last 7 days compared with the same 7 days last week in Gujarat, and which reps had the biggest fall?
97. Explain why EL category sales fell in the North zone in Q2 vs Q1: was it fewer outlets, smaller orders, or fewer calls?
98. Why did new outlet additions slow down in October so far compared with the September average, and in which zones?
99. For the 5 regions with the lowest PC% this month, explain why PC% is low and give 3 actions for each.
100. Why was Monday's productive call count the lowest of last week, and which zones and reps were most affected?

## Multi-line & Hinglish

101. Take the last 60 days. / Find the top 10 reps by secondary sales in the South zone. / Show their PC%, calls per day, and drop size. / Then tell me which of them improved compared with the previous 60 days.
102. Pichle 90 din ke top 10 distributors dikhao North zone mein, aur batao kis distributor ki primary sale 15% se zyada badhi lekin secondary sale gir gayi.
103. इस महीने East zone के bottom 5 reps कौन हैं PC% के हिसाब से, और पिछले महीने से उनका PC% कितना बदला?
104. Compare this quarter with last quarter: / 1. Primary sales by zone / 2. Secondary sales by zone / 3. PC% by zone / Highlight zones that declined on all three.
105. Last 30 days, Maharashtra only, Figaro brand only. Top 10 distributors by secondary sales, their growth vs the previous 30 days, and their billed outlets.
