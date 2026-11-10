package com.atguigu.java.ai.langchain4j.service.mock;

import com.atguigu.java.ai.langchain4j.service.DoctorInfo;
import com.atguigu.java.ai.langchain4j.service.DoctorService;
import com.atguigu.java.ai.langchain4j.service.SlotAvailability;
import com.atguigu.java.ai.langchain4j.service.SlotService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.time.DayOfWeek;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * 【Mock】号源（排班）实现 —— 模拟医院 HIS 的号源系统。
 * <p>
 * 规则<b>确定性</b>（同样的科室 + 日期 + 时段永远得到同样的结果），方便联调与回归：
 * <ul>
 *   <li>门诊时间为周一至周五，周末不出诊；</li>
 *   <li>同一科室的医生按顺序轮流值上午 / 下午，保证每个时段都有医生出诊；</li>
 *   <li>每位医生当天的号源数在 1~5 之间，由医生姓名散列决定；</li>
 *   <li>约 10% 的概率该医生当日号源已约满，用于覆盖“号源已满”的分支。</li>
 * </ul>
 * 完全在本地内存中计算，<b>不会访问任何外部系统</b>。
 * <p>
 * 真实改造时，新增 {@code HisSlotService implements SlotService} 调用医院接口并替换 Bean 装配即可。
 */
public class MockSlotService implements SlotService {

    private static final Logger log = LoggerFactory.getLogger(MockSlotService.class);

    /** 每位医生当日最多可约号数 */
    private static final int MAX_QUOTA = 5;

    /** 约满概率（10%） */
    private static final int FULL_RATIO = 10;

    private final DoctorService doctorService;

    public MockSlotService(DoctorService doctorService) {
        this.doctorService = doctorService;
    }

    @Override
    public SlotAvailability check(String department, String date, String time, String doctorName) {
        LocalDate target = parseDate(date);
        if (target == null) {
            return SlotAvailability.none("日期格式无法识别（应为 2026-11-20 这样的格式），请重新提供就诊日期。");
        }
        String slot = normalizeTime(time);
        if (slot == null) {
            return SlotAvailability.none("时段只能是“上午”或“下午”，请确认后再预约。");
        }
        if (target.getDayOfWeek() == DayOfWeek.SATURDAY || target.getDayOfWeek() == DayOfWeek.SUNDAY) {
            return SlotAvailability.none(department + " 在 " + date + " 是周末，门诊不出诊，请改约工作日。");
        }

        List<DoctorInfo> candidates = doctorService.listDoctors(department);
        if (candidates.isEmpty()) {
            return SlotAvailability.none("本院暂无【" + department + "】这个科室，请确认科室名称，或让我帮你推荐合适的科室。");
        }

        List<DoctorInfo> onDuty = new ArrayList<>();
        for (int i = 0; i < candidates.size(); i++) {
            DoctorInfo doctor = candidates.get(i);
            if (isOnDuty(doctor, i, candidates.size(), slot)) {
                onDuty.add(doctor);
            }
        }

        // 指定了医生，就只保留该医生
        if (doctorName != null && !doctorName.isBlank()) {
            onDuty.removeIf(d -> !d.name().contains(doctorName.trim()));
            if (onDuty.isEmpty()) {
                return SlotAvailability.none("医生【" + doctorName + "】在 " + date + " " + slot + "不出诊，可预约的医生有："
                        + joinNames(candidates) + "。");
            }
        }

        if (onDuty.isEmpty()) {
            return SlotAvailability.none(department + " 在 " + date + " " + slot + "暂无出诊安排。");
        }

        // 逐个医生计算剩余号，剔除已约满的医生
        List<String> availableDoctors = new ArrayList<>();
        int remaining = 0;
        for (DoctorInfo doctor : onDuty) {
            int quota = quotaOf(doctor, target);
            if (quota > 0) {
                availableDoctors.add(doctor.name());
                remaining += quota;
            }
        }

        if (availableDoctors.isEmpty()) {
            return SlotAvailability.none(department + " 在 " + date + " " + slot + "的号源已全部约满，"
                    + "建议换个日期或时段再试。");
        }

        String message = department + " 在 " + date + " " + slot + "有号源，共 " + remaining + " 个号，"
                + "出诊医生：" + join(availableDoctors) + "。";
        log.info("[Mock] 号源查询：{} {} {} -> 出诊医生 {}，剩余 {} 个号",
                department, date, slot, availableDoctors, remaining);
        return new SlotAvailability(true, message, availableDoctors, remaining);
    }

    /**
     * 出诊时段规则：同一科室内医生按顺序轮流值上午 / 下午；
     * 科室只有一位医生时，该医生两个时段都出诊。
     */
    private boolean isOnDuty(DoctorInfo doctor, int index, int total, String slot) {
        if (total == 1) {
            return true;
        }
        boolean morning = index % 2 == 0;
        return "上午".equals(slot) == morning;
    }

    /** 该医生当天的剩余号源数，0 表示已约满 */
    private int quotaOf(DoctorInfo doctor, LocalDate date) {
        int hash = Math.abs(Objects.hash(doctor.name(), date.toString()));
        if (hash % FULL_RATIO == 0) {
            return 0; // 当日号源已约满
        }
        return 1 + hash % MAX_QUOTA;
    }

    private String joinNames(List<DoctorInfo> doctors) {
        return join(doctors.stream().map(DoctorInfo::name).toList());
    }

    private String join(List<String> names) {
        return names.stream().reduce((a, b) -> a + "、" + b).orElse("无");
    }

    private LocalDate parseDate(String date) {
        if (date == null || date.isBlank()) {
            return null;
        }
        try {
            return LocalDate.parse(date.trim());
        } catch (DateTimeParseException e) {
            return null;
        }
    }

    private String normalizeTime(String time) {
        if (time == null || time.isBlank()) {
            return null;
        }
        // 只接受“上午 / 下午”（含“早上 / 早上门诊”等常见说法），“晚上”“中午”不属于门诊时段
        if (time.contains("上午") || time.contains("早上")) {
            return "上午";
        }
        if (time.contains("下午")) {
            return "下午";
        }
        return null;
    }
}
