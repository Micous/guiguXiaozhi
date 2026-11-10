package com.atguigu.java.ai.langchain4j;

import com.atguigu.java.ai.langchain4j.service.DoctorInfo;
import com.atguigu.java.ai.langchain4j.service.DoctorService;
import com.atguigu.java.ai.langchain4j.service.SlotAvailability;
import com.atguigu.java.ai.langchain4j.service.SlotService;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import java.util.List;

/**
 * 号源 Mock 与专家名录（向量库真实检索）的行为校验。
 */
@SpringBootTest
public class MockMedicalServiceTest {

    @Autowired
    private SlotService slotService;

    @Autowired
    private DoctorService doctorService;

    @Test
    public void doctorRosterFromVectorStore() {
        System.out.println("### 科室列表：" + doctorService.listDepartments());
        List<DoctorInfo> neuro = doctorService.listDoctors("神经内科");
        System.out.println("### 神经内科专家数：" + neuro.size());
        neuro.forEach(d -> System.out.println("###   " + d.brief() + " | " + d.expertise()));
        System.out.println("### 模糊匹配“神经内科门诊” -> " + doctorService.listDoctors("神经内科门诊").size() + " 位");
        System.out.println("### 查“王笠” -> " + doctorService.findDoctor("神经内科", "王笠"));
    }

    @Test
    public void slotBehaviour() {
        // 工作日
        for (String date : List.of("2026-11-20", "2026-11-23", "2026-11-24")) {
            SlotAvailability am = slotService.check("神经内科", date, "上午", null);
            SlotAvailability pm = slotService.check("神经内科", date, "下午", null);
            System.out.println("### " + date + " 上午 -> available=" + am.available() + " | " + am.message());
            System.out.println("### " + date + " 下午 -> available=" + pm.available() + " | " + pm.message());
        }
        // 确定性：同样的入参必须得到同样的结果
        SlotAvailability a1 = slotService.check("神经内科", "2026-11-20", "上午", null);
        SlotAvailability a2 = slotService.check("神经内科", "2026-11-20", "上午", null);
        System.out.println("### 确定性校验：" + a1.message().equals(a2.message()));
        // 异常分支
        System.out.println("### 不存在的科室 -> " + slotService.check("核医学科", "2026-11-20", "上午", null).message());
        System.out.println("### 非法日期 -> " + slotService.check("神经内科", "下周一", "上午", null).message());
        System.out.println("### 非法时段 -> " + slotService.check("神经内科", "2026-11-20", "晚上", null).message());
        System.out.println("### 周末 -> " + slotService.check("神经内科", "2026-11-21", "上午", null).message());
        System.out.println("### 指定不出诊的医生 -> "
                + slotService.check("神经内科", "2026-11-20", "上午", "不存在的医生").message());
    }
}
